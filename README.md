# cdcwater · CDC 乱序去重与水位线推进

面向数据集成组的 CDC 合并工具：把订单 / 客户 / 库存三库的变更事件（乱序、重复、迟到）
合并成可信的最终行，给出可复现的水位线，并在放行 schema 变更前逐条评估影响。
整包重放按「来源 + 内容指纹」去重，同版本不同内容隔离待裁决，三重核对一致后原子替换现网。

- 形态：单服务 FastAPI + Jinja2 服务端模板 + 原生 JS，SQLite 命名卷持久化。
- 端口：宿主机 `18213`（容器内 8000）。
- 样例：3 张表 × 60 条事件，故意埋 8 条迟到、3 条同版本冲突、2 条墓碑后迟到、1 条同内容重复。
- 重放样例包：源库压缩历史后的整包重放（行号整体前移 + 4 条新事件 + 1 条同版本不同内容修正）。

## 能力

1. **事件归档**：JSONL 逐行解析、字段校验，事件保留「文件 + 行号」作为定位依据；导入幂等——按「来源 + 内容指纹 + 同指纹序号」去重，同一文件重放 / 行号重排都不会重复导入。
2. **同键合并**：同 pk 保留最大 `version`；同版本同内容静默去重；同版本不同内容判 `conflict` 且不覆盖先到内容。
3. **墓碑处理**：`op=d` 且版本更高 → 键判删除，墓碑保留；墓碑后低版本 c/u 忽略并计入 `ignored_after_tombstone`；只有严格更高版本才复活。
4. **乱序与水位线**：按到达顺序判定迟到（`ts < 当前已处理最大 ts`），迟到仍按版本合并；`watermark = min(每个键已处理事件的最大 ts)`，输出全局/分表水位线、未闭合键数与推进轨迹。
5. **Schema 门禁**：对比 v1 → v2，逐条给「可放行 / 放行+回填 / 阻断」，附列位置与理由。
6. **快照导出**：每表最终行 + 墓碑 + 冲突明细 + 迟到位置 + 门禁结论 + 全部变更日志，JSON 可下载。
7. **整包重放与原子发布**：按「来源 + 内容指纹」识别重复（与行号无关）；同来源同版本不同内容的事件隔离待裁决（可裁决保留归档 / 采用到来）；来源、归档、候选三重核对一致且隔离清零后，在一个事务里原子替换现网快照；失败保留旧版，重跑同一文件即从已归档位置续跑。

## 核心规则口径

| 规则 | 口径 |
|---|---|
| 最大版本 | 同 pk 只保留 `version` 最大的事件；冲突时保留**先到**内容 |
| 同版本 | `after` 按字段排序后逐字比较：一致 → 静默去重（不计冲突）；不一致 → 冲突（列出两条事件与差异字段） |
| 墓碑 | 删除事件写入后不可丢；`version < 墓碑版本` 的事件一律忽略，且不改状态 |
| 迟到 | 文件行序处理，`event.ts < 当前已处理最大 ts`（相等不算迟到），记录表名 + 行号 + 当时最大 ts |
| 水位线 | `min(每个键已处理事件的最大 ts)`；`键最大 ts <= 水位线` 为已闭合键，其余为未闭合键 |
| 门禁 | 新增 nullable → `compatible`；新增带默认值 → `compatible_with_backfill`；删列 / 数值收窄 / 类型族不兼容 / 新增非空无默认 / 可空收紧为非空无默认 → `blocked`；总判定取最严 |
| 重放去重 | 指纹 = sha256(表/键/版本/op/事件时间/schema 版本/after 归一化)；同 `(source, fingerprint, occur)` 判重复，与行号无关 |
| 隔离 | 同来源、同 (表, 键, 版本) 但指纹不同 → 隔离待裁决，不进归档、不进候选；裁决「采用到来」会原位替换归档内容 |
| 三重核对 | 来源：文件每行都有去向且归档期间未被改动（sha256 复核）；归档：非隔离事件逐条在库、各来源增量一致；候选：覆盖归档全部事件、新归档事件全部进入候选 |
| 原子替换 | 三重核对 + 隔离清零全过 → 单事务作废旧现网、写入新现网；候选与现网一致 → 不替换（`unchanged`）；任何失败 → 保留旧版（`blocked`/`failed`） |

## 启动

```bash
cd batch-20261114b/projects/gb-624
cp .env.example .env            # 可选，默认端口 18213
docker compose up -d --build
docker compose ps               # 等 healthy
curl -s http://127.0.0.1:18213/health
```

打开 <http://127.0.0.1:18213/>。SQLite 落在命名卷 `cdcwater-data:/app/data`；
重建数据：`docker compose down -v` 后重新 `up`（启动时幂等 seed，重复启动不会重复导入事件）。

## 演示步骤

1. 首页看样例概览与埋点说明（8 迟到 / 3 冲突 / 2 墓碑后迟到 / 1 重复）。
2. 工作台勾选 3 张表、目标 schema 选 v2，点「执行合并」，看进度与即时摘要。
3. 结果页：每表合并行数、冲突逐条（两条事件行号 + 差异字段）、迟到位置、水位线、门禁逐条结论、快照预览。
4. 结果页或复核页下载快照 JSON；复核页可按结论筛选 180 条变更日志。
5. 整包重放（模块 7）：
   - 「执行重放」跑样例重放包 → 3 条新订单 + 1 条新库存事件归档，1 条同版本不同内容被隔离，
     其余按内容指纹判重复（跳过原因含行号前移对照）；因隔离未裁决 → 核对不通过、保留旧版。
   - 在「隔离事件」里裁决（保留归档 / 采用到来）→ 重跑同一包 → 已归档的判重复、已裁决的跳过，
     三重核对通过 → 原子发布现网快照；再重跑 → 候选与现网一致，不替换。
   - 上传单个 JSONL 文件可重放单表；重传同一文件不会重复追加事件。

## API / 页面

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/health` | 健康检查（含事件数、运行数、待裁决隔离数、现网版本） |
| GET | `/` `/workbench` `/result/{run_id}` `/review` `/replay` | 5 个页面 |
| POST | `/api/merge` | `{"tables":["orders"],"schema_version":2}` → run_id + 摘要 |
| GET | `/api/runs` `/api/runs/{run_id}` | 历史运行 / 单次运行详请 |
| GET | `/api/runs/{run_id}/snapshot` | 下载快照 JSON（attachment） |
| POST | `/api/seed` | 幂等重放样例（已归档事件不重复插入） |
| GET | `/api/samples` `/api/schemas/{1\|2}` | 样例概览 / schema 文档 |
| POST | `/api/replay` | `{"package":"replay","schema_version":2}` → 整包重放样例包 |
| POST | `/api/replay/upload` | multipart 上传单个 JSONL 重放（`file` + 可选 `table`） |
| GET | `/api/replays` `/api/replays/{replay_id}` | 重放历史 / 单次重放详情（含三重核对与替换前后对照） |
| GET | `/api/quarantine` | 隔离事件列表（`?status=pending&table=orders` 过滤） |
| POST | `/api/quarantine/{id}/resolve` | `{"decision":"keep_archived"\|"use_incoming"}` 裁决隔离 |
| GET | `/api/publication` `/api/publication/snapshot` | 当前现网摘要 / 下载现网快照 JSON |

curl 验收：

```bash
RUN=$(curl -s -X POST http://127.0.0.1:18213/api/merge -H 'Content-Type: application/json' \
  -d '{"tables":["orders","customers","inventory"],"schema_version":2}' | python -c 'import json,sys;print(json.load(sys.stdin)["run_id"])')
curl -s http://127.0.0.1:18213/api/runs/$RUN | python -m json.tool
curl -s -o /tmp/cdcwater-snapshot.json http://127.0.0.1:18213/api/runs/$RUN/snapshot
curl -s -o /dev/null -w '%{http_code}\n' -X POST http://127.0.0.1:18213/api/merge \
  -H 'Content-Type: application/json' -d '{"tables":["ghost"],"schema_version":2}'   # 400
```

curl 验收（整包重放）：

```bash
# 第一次重放：新事件归档、1 条隔离 → blocked（保留旧版）
curl -s -X POST http://127.0.0.1:18213/api/replay -H 'Content-Type: application/json' \
  -d '{"package":"replay"}' | python -m json.tool
# 裁决隔离（采用到来），再重跑同一包 → published；再跑 → unchanged
QID=$(curl -s 'http://127.0.0.1:18213/api/quarantine?status=pending' | python -c 'import json,sys;print(json.load(sys.stdin)["quarantine"][0]["quarantine_id"])')
curl -s -X POST http://127.0.0.1:18213/api/quarantine/$QID/resolve \
  -H 'Content-Type: application/json' -d '{"decision":"use_incoming"}'
curl -s -X POST http://127.0.0.1:18213/api/replay -H 'Content-Type: application/json' -d '{"package":"replay"}'
curl -s http://127.0.0.1:18213/api/publication | python -m json.tool          # 当前现网
curl -s -o /tmp/cdcwater-live.json http://127.0.0.1:18213/api/publication/snapshot
```

## 测试

```bash
python -m pytest -q                               # 本地
docker compose run --rm app python -m pytest -q    # 容器内
```

- `tests/test_merge.py`：去重、同版本冲突/同内容、墓碑、墓碑后忽略、复活、迟到仍按版本合并、样例埋点对齐。
- `tests/test_watermark.py`：迟到判定与位置、水位线公式、未闭合键、跨表合并、推进轨迹单调性。
- `tests/test_schema_gate.py`：三类结论矩阵 + 样例 v1→v2 全量（3 可放行 / 1 回填 / 5 阻断）。
- `tests/test_api.py`：成功、400/404/422、端到端（样例 → 合并 → 结论 → 快照）。
- `tests/test_replay.py`：内容指纹与行号无关、行号前移不新增、新订单不被行号误判、
  隔离幂等与两种裁决、三重核对拦截、原子替换失败保留旧版、续跑不重复追加、
  候选一致不替换、旧库迁移、上传重放与 4xx。

## 目录

```text
app/
├── main.py          # FastAPI 路由（薄）
├── settings.py      # 路径、表清单、文案映射
├── archive.py       # 模块1 事件归档（解析/校验/内容指纹）
├── merge.py         # 模块2 同键合并
├── tombstone.py     # 模块3 墓碑/版本判定表（纯函数）
├── watermark.py     # 模块4 迟到与水位线
├── schema_gate.py   # 模块5 schema 门禁
├── snapshot.py      # 模块6 快照导出与页面预览
├── replay.py        # 模块7 整包重放：指纹去重/隔离/三重核对/原子替换
├── runner.py        # 编排 + 落库
├── store.py         # SQLite（events / quarantine / replay_runs / publications / runs / meta）
├── templates/       # base/index/workbench/result/review/replay/not_found
└── static/          # app.js / style.css（原生，无框架）
sample_data/
├── generate_samples.py   # 确定性生成器（末尾自检 8 条迟到）
├── generate_replay.py    # 重放样例包生成器（压缩历史 + 新事件 + 同版本修正）
├── events/{orders,customers,inventory}.jsonl
├── replay/{orders,customers,inventory}.jsonl   # 源库压缩后的整包重放包
└── schemas/{v1,v2}.json
tests/                      # pytest
```

## 已知限制

- 事件只支持 JSONL 落盘后的批量合并，不做实时流接入；重复跑同一批事件会得到同一结果（幂等）。
- 冲突只判定「同版本不同内容」，不做字段级自动合并；合并层冲突需人工重放修正事件，
  重放层的同版本不同内容走隔离区裁决（保留归档 / 采用到来）。
- 重放去重按「来源 + 内容指纹」：同一事件换文件名重放会被视为新来源的新事件（来源是身份的一部分）。
- 水位线按到达顺序的前缀计算，未做事件时间的乱序窗口缓冲（迟到只标注不阻塞）。
- 类型门禁覆盖 string/int/long/decimal/timestamp/boolean 的收窄与族不兼容，不做列重命名识别。
- 队列/上游 connector、鉴权、多租户不在本演示范围。
