# CareAgent MCP 能力层

> MCP（Model Context Protocol）把护理能力变成标准工具：外部 Agent / 系统不用学我们的 REST 接口，按 MCP 协议即可调用；将来 care_agent 也能反向消费外部 MCP Server（医院 HIS、排班系统等）。
>
> 只读工具直接可用；写工具（改知识库、派工单）需要 `MCP_ADMIN_TOKEN`，并按租户白名单放行。

## 一、端点

所有 MCP Server 与主应用**同进程挂载**（`backend/main.py`），共用 8000 端口，工具端点为 `POST /mcp/<name>/mcp`。

中文名 ↔ 服务名 ↔ 用途对照（服务名是配置里要写的路径，中文名是它实际干的事）：

| 中文名 | 服务名（路径） | 干什么 | 工具数 | 鉴权 | 独立端口 |
|---|---|---|---|---|---|
| 护理知识库检索 | `/mcp/sop-kb` | 查护理 SOP 知识库（混合召回 + 精排），列文档、看知识库状态 | 3（只读） | 无 | 8011 |
| 照护数据查询 | `/mcp/care-data` | 查老人档案、照护记录、健康指标、设备读数、告警列表 | 5（只读） | 无 | 8012 |
| 联网搜索 | `/mcp/web-search` | 知识库覆盖不到时搜互联网（Tavily → DuckDuckGo 兜底） | 1（只读） | 无 | 8013 |
| 知识库管理 | `/mcp/sop-kb-admin` | 导入/更新 SOP 文档、删除文档、重建集合 | 3（写） | Bearer Token | 8015 |
| 告警触发与派单 | `/mcp/alert-ops` | 触发告警研判、读审核数据、站长确认派工单、列待审核告警 | 4（写） | Bearer Token | 8014 |
| 照护流程 | `/mcp/care-flow` | 启动照护流程、下发退住工单、查会话状态、取报告 | 4（写） | Bearer Token | 8016 |

独立运行（`python -m backend.mcp.sop_kb_server`）只建议在需要单独扩缩容时使用：BGE-M3 与 BGE-Reranker 是进程内单例，各约占 2-3GB，拆进程会各加载一份，内存翻倍。

在 `.env.local` 里设 `MCP_ENABLED=false` 可整体关闭挂载。

## 二、工具清单

### 只读 · 护理知识库检索（`sop-kb`）

> 完整参数表（含类型与必填性）见 [工具与节点清单](工具清单.md)；机器可读定义（JSON Schema）见 `docs/mcp_tools_schema.json`，
> 由 `python scripts/dump_mcp_tools.py` 自动导出。

| 工具 | 参数 | 返回 | 包装的现有代码 |
|---|---|---|---|
| `search_sop`（检索 SOP 片段） | `query`、`tenant_id`、`category?`、`top_k=3` | 片段列表：`content` / `source_name` / `category` / `score` / `confidence` / `is_high_confidence` | `backend/core/reranker.py:retrieve()`（Hybrid 召回 + BGE 精排，阈值 0.75） |
| `list_sop_documents`（列 SOP 文档） | `tenant_id` | 文档清单：`document_id` / `source_name` / `category` / `version` / `chunks` | Milvus 按租户聚合 |
| `get_kb_status`（看知识库状态） | `tenant_id` | `{collection, exists, tenant_id, chunks}` | Milvus `has_collection` + `count(*)` |

### 只读 · 照护数据查询（`care-data`）

| 工具 | 参数 | 返回 | 包装的现有代码 |
|---|---|---|---|
| `get_elder_profile`（查老人档案） | `elder_id`、`tenant_id` | 老人信息 + 最近照护计划 + 绑定设备 | `elders` / `care_plans` / `devices` |
| `get_recent_care_records`（查照护记录） | `elder_id`、`tenant_id`、`limit=8` | 最近照护记录（告警第二轨） | `backend/db/iot_repo.py` |
| `get_recent_health_indicators`（查健康指标） | `elder_id`、`tenant_id`、`limit=8` | 最近健康指标（告警第四轨） | `backend/db/iot_repo.py` |
| `get_recent_device_readings`（查设备读数） | `elder_id`、`tenant_id`、`limit=10` | 最近设备读数（告警第一轨） | `backend/db/iot_repo.py` |
| `list_alerts`（查告警列表） | `tenant_id`、`elder_id?`、`limit=10` | 告警列表（时间倒序） | `alerts` 查询 |

### 只读 · 联网搜索（`web-search`）

| 工具 | 参数 | 说明 |
|---|---|---|
| `web_search`（联网搜索） | `query`、`max_results=5` | 包装 `backend/core/web_search.py`：Tavily → DuckDuckGo 兜底 |

### 写 · 知识库管理（`sop-kb-admin`，需 Token）

| 工具 | 参数 | 说明 |
|---|---|---|
| `ingest_sop_document`（导入/更新 SOP） | `file_path`、`category`、`tenant_id`、`version="1.0"`、`use_context=true` | 包装 `scripts/build_knowledge_base.py`；`file_path` 必须在 `data/` 目录内；导入后自动 flush，做到"导入即可查" |
| `delete_sop_document`（删除 SOP 文档） | `document_id`、`tenant_id` | 先校验文档属于该租户再删除；跨租户/不存在统一返回 `not_found`；删除后 flush |
| `rebuild_collection`（重建知识库集合） | `tenant_id`、`confirm` | 包装 `scripts/init_milvus.py`，**drop 整个集合（影响所有租户）**，必须显式传 `confirm="DROP_AND_REBUILD"` |

### 写 · 告警触发与派单（`alert-ops`，需 Token）

| 工具 | 参数 | 对应 REST |
|---|---|---|
| `trigger_alert`（触发告警研判） | `elder_id`、`device_id`、`tenant_id`、`alert_type`、`alert_timestamp?` | `POST /api/v1/alert/trigger`：跑四轨采集 + 风险评估，在站长审核处暂停 |
| `get_alert_review_data`（读审核数据） | `alert_id`、`tenant_id` | `GET /api/v1/alert/{id}/review` |
| `confirm_alert`（站长确认派单） | `alert_id`、`tenant_id`、`action`、`modified_actions?`、`comment?`、`supervisor_id?` | `POST /api/v1/alert/{id}/confirm`：恢复图执行并派发工单 |
| `list_pending_alerts`（列待审核告警） | `tenant_id`、`limit=10` | 列出 `status=reviewing` 的告警（MCP 新增能力） |

### 写 · 照护流程（`care-flow`，需 Token）

| 工具 | 参数 | 对应 REST |
|---|---|---|
| `start_care_flow`（启动照护流程） | `elder_id`、`tenant_id`、`care_plan_id?`、`initial_stage?`、`discharge_order?` | `POST /api/v1/care-flow/start` |
| `send_discharge_order`（下发退住工单） | `session_id`、`tenant_id`、`order`（dict） | `POST /api/v1/care-flow/sessions/{sid}/discharge-order` |
| `get_care_flow_status`（查会话状态） | `session_id`、`tenant_id` | `GET /api/v1/care-flow/active/{elder_id}` |
| `get_care_flow_report`（取报告） | `session_id`、`tenant_id` | `GET /api/v1/care-flow/{sid}/report` |

> 与 `alert-ops` 的差异：告警是"无状态一次性触发"（`trigger_alert` 新建 `alert_id` 再跑图）；照护流程是"有状态多轮会话"，所以 `send_discharge_order` **只往已有 thread 的检查点写 `state.discharge_order`，不跑图、不耗 LLM**，等护理员下一次对话时由 `check_stage_node` 识别并触发阶段收敛（复评 → 出院评估）。MCP 与 REST 两个入口共用 `backend/agents/care_flow/discharge.py:deliver_discharge_order`，落地逻辑只有一份。

## 三、鉴权与安全

写工具的 Token 在 `.env.local` 配置：

```bash
MCP_ADMIN_TOKEN=<随机串>        # 留空 = 写工具一律 503（fail-closed）
MCP_WRITE_TENANTS=tenant_default  # 写工具允许的租户白名单（逗号分隔）
```

保护方式：`backend/mcp/auth.py` 的 `BearerTokenMiddleware` 包在写 Server 外层，`tools/list` 和 `tools/call` 都要过门禁；工具内部再用 `assert_tenant_allowed()` 校验租户。其余约定：

- 所有工具的 `tenant_id` 必填，SQL 与 Milvus 查询强制按租户过滤，跨租户统一按"不存在"返回。
- `ingest_sop_document` 只允许 `data/` 目录下的 `.md/.txt`，防止调用方读走服务器任意文件。
- `confirm_alert` 会先确认告警属于该租户，再恢复图；跨租户直接返回不存在。

## 四、怎么调用

```bash
python scripts/check_mcp.py                    # 全部 server 冒烟：列工具 + 调真实工具 + 校验 401
python scripts/check_mcp.py --server write     # 只验写工具的鉴权开关
python scripts/check_mcp.py --base http://localhost:8011   # 指向独立运行的 server
```

curl（原生 JSON-RPC，stateless 调用无需握手）：

```bash
# 只读：查老人档案
curl -s -X POST http://localhost:8000/mcp/care-data/mcp \
  -H 'Content-Type: application/json' -H 'Accept: application/json' \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{
        "name":"get_elder_profile",
        "arguments":{"elder_id":"e001","tenant_id":"tenant_default"}}}'

# 写：触发告警（需 Token）
curl -s -X POST http://localhost:8000/mcp/alert-ops/mcp \
  -H 'Content-Type: application/json' -H 'Accept: application/json' \
  -H "Authorization: Bearer $MCP_ADMIN_TOKEN" \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{
        "name":"trigger_alert",
        "arguments":{"elder_id":"e001","device_id":"dev-001","tenant_id":"tenant_default"}}}'
```

Python 客户端（`backend/mcp/client.py`）：

```python
from backend.mcp.client import call_mcp_tool, list_mcp_tools

profile = await call_mcp_tool("http://localhost:8000/mcp/care-data",
                             "get_elder_profile",
                             {"elder_id": "e001", "tenant_id": "tenant_default"})

alert = await call_mcp_tool("http://localhost:8000/mcp/alert-ops", "trigger_alert",
                            {"elder_id": "e001", "device_id": "dev-001",
                             "tenant_id": "tenant_default"},
                            timeout=240.0, token="<MCP_ADMIN_TOKEN>")
```

## 五、实现要点（踩过的坑）

| 问题 | 处理 |
|---|---|
| mount 不会传播子应用 lifespan | 主应用 lifespan 里用 `AsyncExitStack` 手工进入各子应用的 `lifespan_context` |
| 门禁中间件挡住 lifespan | `BearerTokenMiddleware.__getattr__` 把 `.router` 等属性透传给底层 ASGI 应用 |
| FastMCP 把 `list[dict]` 塞进多条 `TextContent` | 客户端遍历全部 `content` 解析，而不是只取 `content[0]` |
| 工具内部异常走 `isError` 通道而非 JSON-RPC error | 客户端检测 `isError` 并显式抛错，避免错误文本被当成正常数据 |
| 本机代理劫持 localhost 调用 | httpx 一律 `trust_env=False` |
| 告警"确认了但没派单" | 恢复读档必须和暂停存档用同一个 thread_id（存档槽名）：确认工具已改为 `build_config(elder_id, alert_id)`，与触发时一致；REST 与 MCP 两条入口都已统一 |
| Milvus 写入/删除后查询看不到 | 管理工具在 ingest/delete 后调用底层连接 `flush`（pymilvus 2.4.9 的 `MilvusClient` 没有 `flush()`） |

## 六、Roadmap

| 优先级 | 事项 | 说明 |
|---|---|---|
| P1 | 反向消费外部 MCP | 用 `langchain-mcp-adapters` 把医院 HIS、排班系统等外部 MCP 挂成 Agent 工具，公共层从"静态能力"变成"可插拔工具" |
| P2 | `assessment` server | `compute_weighted_score`（五维 0.30/0.25/0.20/0.15/0.10）、`generate_care_plan_from_assessment` |
| P2 | MCP resources / prompts | 用 `sop://documents/{id}` 暴露 SOP 原文资源，用 `prompts` 固化护理问答模板 |
| P3 | 写工具审计日志 | 记录 Token 使用者、目标租户、变更内容，便于事后追溯 |

## 七、已知限制

- 运行环境必须 **Python 3.11+**：3.10 上 LangGraph `interrupt()` 报 `Called get_config outside of a runnable context`，告警写工具（`trigger_alert`/`confirm_alert`）会退化成 fallback。
- `pymilvus 2.4.9` 依赖 `pkg_resources`，需要 `setuptools<81`（requirements 已锁）。
- `search_sop` / `ingest_sop_document` 首次调用要加载 BGE-M3（约 30s-2min），属正常现象。
