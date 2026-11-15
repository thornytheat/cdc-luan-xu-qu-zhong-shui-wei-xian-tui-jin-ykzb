"""FastAPI 路由（薄）：页面渲染 + JSON 端点，业务全部在领域模块里。

页面：/ 首页 · /workbench 工作台 · /result/{run_id} 结果 · /review 复核 · /replay 整包重放
接口：POST /api/merge · GET /api/runs · GET /api/runs/{id} · GET /api/runs/{id}/snapshot
      POST /api/replay · POST /api/replay/upload · GET /api/replays · GET /api/replays/{id}
      POST /api/quarantine/{id}/resolve · GET /api/publication · GET /api/publication/snapshot
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field

from . import archive, replay, runner, schema_gate, settings, snapshot, store
from .archive import EventError

BASE_DIR = settings.BASE_DIR


@asynccontextmanager
async def lifespan(app: FastAPI):
    conn = store.connect()
    store.init_db(conn)
    if store.total_events(conn) == 0:
        store.seed_sample(conn)
    conn.close()
    yield


app = FastAPI(title=settings.APP_NAME, lifespan=lifespan)
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "app" / "static")), name="static")
templates = Jinja2Templates(directory=str(BASE_DIR / "app" / "templates"))


class MergeRequest(BaseModel):
    tables: list[str] = Field(default_factory=list, description="要合并的表名")
    schema_version: int = Field(2, ge=1, le=2, description="要评估的目标 schema 版本")


class ReplayRequest(BaseModel):
    package: str = Field("replay", description="重放包名（目前内置 replay）")
    schema_version: int = Field(2, ge=1, le=2, description="候选结果评估的目标 schema 版本")


class ResolveRequest(BaseModel):
    decision: str = Field(..., description="keep_archived 保留归档 / use_incoming 采用到来")


def render(request: Request, template: str, **ctx) -> HTMLResponse:
    context = {
        "app_name": settings.APP_NAME,
        "app_short": settings.APP_SHORT,
        "tables_meta": settings.TABLES,
        "kind_labels": settings.KIND_LABELS,
        "verdict_labels": settings.VERDICT_LABELS,
        "change_labels": settings.CHANGE_LABELS,
        "op_labels": settings.OP_LABELS,
        "replay_status_labels": replay.STATUS_LABELS,
        "quarantine_status_labels": replay.QUARANTINE_STATUS_LABELS,
        "nav": ctx.pop("nav", ""),
    }
    context.update(ctx)
    return templates.TemplateResponse(request, template, context)


def with_conn():
    conn = store.connect()
    store.init_db(conn)
    return conn


# --- 页面 ------------------------------------------------------------------


@app.get("/health", include_in_schema=False)
def health():
    conn = with_conn()
    try:
        live = store.live_publication(conn)
        return {
            "status": "ok",
            "app": settings.APP_SHORT,
            "events": store.total_events(conn),
            "runs": store.run_count(conn),
            "seed_version": store.get_meta(conn, "seed_version"),
            "replay_runs": store.replay_run_count(conn),
            "pending_quarantine": store.count_quarantine(conn, status="pending"),
            "live_publication": live["publication_id"] if live else None,
        }
    finally:
        conn.close()


@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    conn = with_conn()
    try:
        overview = store.event_overview(conn)
        meta = {
            "seed_version": store.get_meta(conn, "seed_version"),
            "seeded_at": store.get_meta(conn, "seeded_at"),
            "total_events": store.total_events(conn),
            "runs": store.run_count(conn),
        }
        recent = store.list_runs(conn, limit=3)
    finally:
        conn.close()
    stats = {
        "total_events": meta["total_events"],
        "table_count": len(settings.TABLES),
        "runs": meta["runs"],
        "last_run_id": recent[0]["run_id"] if recent else None,
        "last_run_at": recent[0]["created_at"] if recent else None,
        "last_conflicts": recent[0]["summary"]["conflicts"] if recent else 0,
    }
    return render(
        request,
        "index.html",
        page="index",
        nav="index",
        overview=overview,
        meta=meta,
        recent=recent,
        stats=stats,
        embedded=settings.EMBEDDED,
        steps=settings.DEMO_STEPS,
        schema_v1=schema_gate.load_schema(settings.SCHEMAS_DIR / "v1.json"),
        schema_v2=schema_gate.load_schema(settings.SCHEMAS_DIR / "v2.json"),
        changes=schema_gate.structural_changes(
            schema_gate.load_schema(settings.SCHEMAS_DIR / "v1.json"),
            schema_gate.load_schema(settings.SCHEMAS_DIR / "v2.json"),
            settings.TABLE_NAMES,
        ),
    )


@app.get("/workbench", response_class=HTMLResponse)
def workbench(request: Request):
    conn = with_conn()
    try:
        overview = store.event_overview(conn)
        recent = store.list_runs(conn, limit=5)
    finally:
        conn.close()
    return render(
        request,
        "workbench.html",
        page="workbench",
        nav="workbench",
        overview=overview,
        recent=recent,
        schema_v1=schema_gate.load_schema(settings.SCHEMAS_DIR / "v1.json"),
        schema_v2=schema_gate.load_schema(settings.SCHEMAS_DIR / "v2.json"),
    )


@app.get("/result/{run_id}", response_class=HTMLResponse)
def result_page(request: Request, run_id: str):
    conn = with_conn()
    try:
        run = store.get_run(conn, run_id)
    finally:
        conn.close()
    if run is None:
        return render(
            request,
            "not_found.html",
            page="not_found",
            nav="",
            run_id=run_id,
            error=f"没有找到运行 {run_id}，可能已被清理；请到工作台重新执行一次合并。",
        )
    snap = snapshot.build_snapshot(run)
    preview = snapshot.preview(snap)
    result = run["result"]
    ignored = {
        table: set(payload.get("ignored_line_nos", []))
        for table, payload in result["tables"].items()
    }
    change_by_line = {(entry["table"], entry["line_no"]): entry for entry in result["changelog"]}
    late_rows = []
    for item in result["late_events"]:
        row = dict(item)
        entry = change_by_line.get((item["table"], item["line_no"]), {})
        row["after_tombstone"] = item["line_no"] in ignored.get(item["table"], set())
        row["kind"] = entry.get("kind", "")
        row["kind_label"] = entry.get("kind_label", "已处理")
        row["reason"] = entry.get("reason", "")
        late_rows.append(row)
    return render(
        request,
        "result.html",
        page="result",
        nav="",
        run=run,
        summary=run["summary"],
        result=result,
        snapshot=snap,
        preview=preview,
        late_rows=late_rows,
        snapshot_bytes=snapshot.snapshot_size(snap),
        base_schema=schema_gate.load_schema(settings.SCHEMAS_DIR / "v1.json"),
        target_schema=schema_gate.load_schema(
            settings.SCHEMAS_DIR / f"v{run['schema_version']}.json"
        ),
    )


@app.get("/review", response_class=HTMLResponse)
def review(request: Request, run_id: str | None = None, kind: str = Query(default="")):
    conn = with_conn()
    try:
        runs = store.list_runs(conn, limit=50)
        selected = store.get_run(conn, run_id) if run_id else (
            store.get_run(conn, runs[0]["run_id"]) if runs else None
        )
    finally:
        conn.close()
    entries: list[dict] = []
    kinds: list[str] = []
    if selected is not None:
        entries = selected["result"]["changelog"]
        kinds = sorted({entry["kind"] for entry in entries})
        if kind:
            entries = [entry for entry in entries if entry["kind"] == kind]
        entries = entries[:200]
    return render(
        request,
        "review.html",
        page="review",
        nav="review",
        runs=runs,
        selected=selected,
        entries=entries,
        kinds=kinds,
        active_kind=kind,
        run_count=len(runs),
    )


@app.get("/replay", response_class=HTMLResponse)
def replay_page(request: Request, replay_id: str | None = None):
    conn = with_conn()
    try:
        runs = store.list_replay_runs(conn, limit=20)
        selected = None
        if replay_id:
            selected = store.get_replay_run(conn, replay_id)
        if selected is None and runs:
            selected = store.get_replay_run(conn, runs[0]["replay_id"])
        live = store.live_publication(conn)
        pending = store.list_quarantine(conn, status="pending")
        quarantine = store.list_quarantine(conn, limit=50)
        publications = store.list_publications(conn, limit=5)
        stats = {
            "events": store.total_events(conn),
            "replay_runs": store.replay_run_count(conn),
            "pending_quarantine": store.count_quarantine(conn, status="pending"),
            "publications": store.publication_count(conn),
        }
    finally:
        conn.close()
    return render(
        request,
        "replay.html",
        page="replay",
        nav="replay",
        runs=runs,
        selected=selected,
        live=live,
        pending=pending,
        quarantine=quarantine,
        publications=publications,
        stats=stats,
        replay_package_exists=settings.REPLAY_DIR.exists(),
    )


# --- API -------------------------------------------------------------------


@app.post("/api/merge")
def api_merge(payload: MergeRequest):
    conn = with_conn()
    try:
        run = runner.execute(conn, payload.tables, payload.schema_version)
    except runner.RunError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except schema_gate.SchemaError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except EventError as exc:
        raise HTTPException(status_code=422, detail=f"样例事件解析失败：{exc}") from exc
    finally:
        conn.close()
    return JSONResponse(
        {
            "run_id": run["run_id"],
            "summary": run["summary"],
            "tables": run["tables"],
            "schema_version": run["schema_version"],
            "result_url": f"/result/{run['run_id']}",
            "snapshot_url": f"/api/runs/{run['run_id']}/snapshot",
        }
    )


@app.get("/api/runs")
def api_runs(limit: int = Query(default=20, ge=1, le=100)):
    conn = with_conn()
    try:
        runs = store.list_runs(conn, limit=limit)
    finally:
        conn.close()
    return {"runs": runs, "count": len(runs)}


@app.get("/api/runs/{run_id}")
def api_run(run_id: str):
    conn = with_conn()
    try:
        run = store.get_run(conn, run_id)
    finally:
        conn.close()
    if run is None:
        raise HTTPException(status_code=404, detail=f"运行不存在：{run_id}")
    result = run["result"]
    return {
        "run_id": run["run_id"],
        "created_at": run["created_at"],
        "tables": run["tables"],
        "schema_version": run["schema_version"],
        "summary": run["summary"],
        "watermark": {
            "watermark": result["watermark"]["watermark"],
            "open_keys": result["watermark"]["open_keys"],
            "closed_keys": result["watermark"]["closed_keys"],
            "per_table": {
                table: {
                    "watermark": payload["watermark"]["watermark"],
                    "open_keys": payload["watermark"]["open_keys"],
                    "closed_keys": payload["watermark"]["closed_keys"],
                    "laggard_keys": payload["watermark"]["laggard_keys"],
                }
                for table, payload in result["tables"].items()
            },
        },
        "tables": {
            table: {
                "row_count": payload["row_count"],
                "tombstone_count": payload["tombstone_count"],
                "key_count": payload["key_count"],
                "counters": payload["counters"],
            }
            for table, payload in result["tables"].items()
        },
        "gate": result["gate"],
        "conflicts": [
            {
                "table": item["table"],
                "pk": item["pk"],
                "version": item["version"],
                "diff": item["diff"],
                "first_line": item["first"]["line_no"],
                "second_line": item["second"]["line_no"],
            }
            for item in result["conflicts"]
        ],
        "late_events": result["late_events"],
        "snapshot_url": f"/api/runs/{run['run_id']}/snapshot",
    }


@app.get("/api/runs/{run_id}/snapshot")
def api_snapshot(run_id: str):
    conn = with_conn()
    try:
        run = store.get_run(conn, run_id)
    finally:
        conn.close()
    if run is None:
        raise HTTPException(status_code=404, detail=f"运行不存在：{run_id}")
    payload = snapshot.dumps(snapshot.build_snapshot(run))
    filename = f"cdcwater-snapshot-{run_id}.json"
    return Response(
        content=payload,
        media_type="application/json; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.post("/api/seed")
def api_seed():
    """幂等重放样例：已归档的事件不会重复插入。"""
    conn = with_conn()
    try:
        stats = store.seed_sample(conn)
        stats["total_events"] = store.total_events(conn)
        stats["counts"] = store.event_counts(conn)
    except EventError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    finally:
        conn.close()
    return stats


@app.get("/api/samples")
def api_samples():
    conn = with_conn()
    try:
        return {"overview": store.event_overview(conn), "total": store.total_events(conn)}
    finally:
        conn.close()


@app.get("/api/schemas/{version}")
def api_schema(version: int):
    if version not in (1, 2):
        raise HTTPException(status_code=400, detail="schema 版本只支持 1 或 2")
    return schema_gate.load_schema(settings.SCHEMAS_DIR / f"v{version}.json")


# --- 整包重放与原子发布 ------------------------------------------------------


def replay_response(run: dict) -> JSONResponse:
    files = {
        f["source"]: {
            "lines": f["lines"],
            "archived_new": f["archived_new"],
            "duplicates": f["duplicates"],
            "quarantined": f["quarantined"],
            "skipped_resolved": f["skipped_resolved"],
        }
        for f in run["files"]
    }
    return JSONResponse(
        {
            "replay_id": run["replay_id"],
            "status": run["status"],
            "status_label": replay.STATUS_LABELS.get(run["status"], run["status"]),
            "package": run["package"],
            "tables": run["tables"],
            "files": files,
            "checks_ok": run["checks"].get("ok") if run["checks"] else None,
            "publication_id": run["publication_id"],
            "error": run["error"],
            "replay_url": f"/replay?replay_id={run['replay_id']}",
        }
    )


@app.post("/api/replay")
def api_replay(payload: ReplayRequest):
    """整包重放内置样例包：归档 → 三重核对 → 原子替换（或保留旧版）。"""
    if payload.package != "replay":
        raise HTTPException(status_code=400, detail=f"未知的重放包：{payload.package}（可选 replay）")
    conn = with_conn()
    try:
        files = replay.sample_package_files()
        run = replay.execute_replay(conn, files, package="replay",
                                    schema_version=payload.schema_version)
    except replay.ReplayError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except EventError as exc:
        raise HTTPException(status_code=422, detail=f"重放文件解析失败：{exc}") from exc
    finally:
        conn.close()
    return replay_response(run)


@app.post("/api/replay/upload")
async def api_replay_upload(
    file: UploadFile = File(...),
    table: str = Form(""),
    schema_version: int = Form(2),
):
    """上传单个 JSONL 事件文件重放；重传同一文件不会重复追加事件。"""
    name = Path(file.filename or "").name
    if not name.endswith(".jsonl"):
        raise HTTPException(status_code=400, detail="只接受 .jsonl 事件文件")
    table = table.strip() or settings.TABLE_BY_FILE.get(name, "")
    if table not in settings.TABLE_NAMES:
        raise HTTPException(
            status_code=400,
            detail=f"无法识别文件 {name} 对应的表；请用 table 字段指定（可选 {', '.join(settings.TABLE_NAMES)}）",
        )
    content = await file.read()
    if not content.strip():
        raise HTTPException(status_code=422, detail="文件为空")
    replay_id = replay.new_replay_id()
    folder = settings.UPLOAD_DIR / replay_id
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / name
    path.write_bytes(content)
    conn = with_conn()
    try:
        run = replay.execute_replay(
            conn, [(path, table)], package=f"upload:{name}",
            schema_version=schema_version, replay_id=replay_id,
        )
    except replay.ReplayError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except EventError as exc:
        raise HTTPException(status_code=422, detail=f"上传文件解析失败：{exc}") from exc
    finally:
        conn.close()
    return replay_response(run)


@app.get("/api/replays")
def api_replays(limit: int = Query(default=20, ge=1, le=100)):
    conn = with_conn()
    try:
        runs = store.list_replay_runs(conn, limit=limit)
    finally:
        conn.close()
    return {
        "runs": [
            {
                "replay_id": run["replay_id"],
                "created_at": run["created_at"],
                "status": run["status"],
                "status_label": replay.STATUS_LABELS.get(run["status"], run["status"]),
                "package": run["package"],
                "tables": run["tables"],
                "publication_id": run["publication_id"],
            }
            for run in runs
        ],
        "count": len(runs),
    }


@app.get("/api/replays/{replay_id}")
def api_replay_detail(replay_id: str):
    conn = with_conn()
    try:
        run = store.get_replay_run(conn, replay_id)
    finally:
        conn.close()
    if run is None:
        raise HTTPException(status_code=404, detail=f"重放运行不存在：{replay_id}")
    return run


@app.get("/api/quarantine")
def api_quarantine(status: str = Query(default=""), table: str = Query(default="")):
    conn = with_conn()
    try:
        records = store.list_quarantine(
            conn,
            status=status or None,
            tables=[table] if table else None,
        )
    finally:
        conn.close()
    return {"quarantine": records, "count": len(records)}


@app.post("/api/quarantine/{quarantine_id}/resolve")
def api_resolve_quarantine(quarantine_id: str, payload: ResolveRequest):
    """裁决隔离事件；裁决后重跑同一重放包即可从已归档位置续跑并发布。"""
    conn = with_conn()
    try:
        record = replay.resolve_quarantine(conn, quarantine_id, payload.decision)
    except replay.ReplayError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except store.StoreError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    finally:
        conn.close()
    return {
        "quarantine": record,
        "hint": "重新执行同一重放包即可从已归档位置续跑（已归档事件不会重复追加）",
    }


@app.get("/api/publication")
def api_publication():
    conn = with_conn()
    try:
        live = store.live_publication(conn)
        publications = store.list_publications(conn, limit=10)
    finally:
        conn.close()
    return {
        "live": None
        if live is None
        else {
            "publication_id": live["publication_id"],
            "created_at": live["created_at"],
            "replay_id": live["replay_id"],
            "tables": live["tables"],
            "summary": live["summary"],
        },
        "history": [
            {
                "publication_id": pub["publication_id"],
                "created_at": pub["created_at"],
                "replay_id": pub["replay_id"],
                "tables": pub["tables"],
                "is_live": pub["is_live"],
                "summary": pub["summary"],
            }
            for pub in publications
        ],
    }


@app.get("/api/publication/snapshot")
def api_publication_snapshot():
    conn = with_conn()
    try:
        live = store.live_publication(conn)
    finally:
        conn.close()
    if live is None:
        raise HTTPException(status_code=404, detail="现网还没有已发布的快照")
    payload = snapshot.dumps(live["snapshot"])
    filename = f"cdcwater-live-{live['publication_id']}.json"
    return Response(
        content=payload,
        media_type="application/json; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
