# CareAgent API 明细文档

> Base URL：`http://localhost:8000`
> 交互式文档：http://localhost:8000/docs（Swagger UI）
>
> 本文件覆盖 **REST + SSE 入口（21 个端点）**。另有一套面向外部 Agent / 系统的 **MCP 入口**（6 个 Server：知识库检索、服务数据、联网搜索、知识库管理、告警派单、作业流程，写工具需 Token），见 [MCP 文档](mcp.md)。两套入口调用同一批底层能力，互不替代。

## 一、通用约定

### 认证

除 `POST /api/v1/auth/login` 与 `GET /health` 外，所有接口需携带 JWT：

```
Authorization: Bearer <access_token>
```

令牌载荷：`sub`（用户ID）、`role`（角色）、`tenant_id`（租户），有效期默认 7 天。

### 角色

| 角色 | 说明 | 权限边界 |
|---|---|---|
| caregiver | 作业人员 | 评估上传、SOP 问答、作业流程、触发告警、IoT 上报 |
| supervisor | 值班主管 | 以上全部 + 告警审核/确认（HitL） |
| admin | 管理员 | 全部权限 |

### 多租户语义

- **读隔离**：所有业务查询强制 `tenant_id` 过滤，跨租户资源返回 `404`（视为不存在）或空列表
- **写隔离**：跨租户点位写入被拒绝（`403`），不会产生跨租户数据
- IoT 上报中点位归属其他租户 → `403 点位不属于当前租户`

### 错误码

| 状态码 | 场景 |
|---|---|
| 400 | 请求参数错误（如非 PDF 上传） |
| 401 | 未登录 / 令牌无效或过期 |
| 403 | 角色不足或跨租户写入 |
| 404 | 资源不存在（含跨租户"视为不存在"） |
| 500 | 服务端错误（如落库失败） |

---

## 二、认证

### POST /api/v1/auth/login

登录并签发 JWT。

**请求体**

```json
{ "username": "supervisor", "password": "Supervisor@123" }
```

**响应 200**

```json
{
  "access_token": "eyJhbGciOi...",
  "token_type": "bearer",
  "role": "supervisor",
  "user_id": "u-supervisor"
}
```

**错误**：401 用户名或密码错误

---

## 三、评估 Agent

### POST /api/v1/assessment/upload?site_id={site_id}

上传点位评估表 PDF，同步触发评估流程（5 维度并行评分 → 汇总 → 服务计划建议），结果落库 `assessment_results`。

**请求**：`multipart/form-data`，字段 `file`（仅 .pdf）；`site_id` 为 query 参数。

**响应 200**

```json
{
  "assessment_id": "asmt-xxxxxxxxxxxx",
  "status": "completed",
  "weighted_score": 62.5,
  "summary": {},
  "dimension_scores": [],
  "issues": []
}
```

### GET /api/v1/assessment/{assessment_id}

查询历史评估结果（直读 `assessment_results`，租户隔离；不存在/跨租户返回 404）。响应结构同上。

### GET /api/v1/assessment/{assessment_id}/export

把评估结果导出为 **Word（.docx）**，用于打印、盖章与归档。

- 响应：`Content-Type: application/vnd.openxmlformats-officedocument.wordprocessingml.document`
- 文件名：`Content-Disposition` 同时给 ASCII 兜底名与 RFC 5987 的中文名
- 文档结构：标题 → 基本信息表（评估编号/点位编号/综合得分/风险等级建议）→ 维度评分表 → 问题清单表 → 整体评价 → 免责声明
- 租户隔离同上：不存在或跨租户返回 404

```bash
curl -H "Authorization: Bearer <token>" \
  -OJ http://localhost:8000/api/v1/assessment/asmt-xxxx/export
```

---

## 四、作业规范 问答 Agent（SSE）

### POST /api/v1/sop-qa/chat

**请求体**

```json
{
  "query": "配电间巡检要注意什么？",
  "site_id": "",
  "session_id": "",
  "org_id": null
}
```

`session_id` 留空则服务端生成（`sopqa-{hex}`）；携带同一 `session_id` 可延续多轮会话。

**SSE 事件序列**：`session` → `answer` → `done`

| 事件 | data 字段 |
|---|---|
| `session` | `{session_id}` |
| `answer` | `{content, answer_mode, sources[]}` — `answer_mode`：knowledge（知识库）/ web（联网搜索）/ direct（直接回答）/ general（通用兜底）；`sources` 为引用来源 |
| `error` | `{error}`（异常时） |
| `done` | 空，流结束 |

---

## 五、告警研判 Agent（HitL 三步流）

### 1. POST /api/v1/alert/trigger — 触发告警

角色：caregiver / supervisor / admin。

**请求体**

```json
{
  "site_id": "elder-001",
  "device_id": "device-hr-01",
  "alert_type": "current_abnormal",
  "alert_timestamp": ""
}
```

**响应 200**：Agent 执行至 `interrupt()` 暂停，返回审核所需数据：

```json
{
  "alert_id": "alert-xxxxxxxxxxxx",
  "session_id": "alert-xxxxxxxxxxxx",
  "status": "awaiting_review",
  "review_data": { "...": "风险评估 + 四轨数据（设备读数 / 服务记录 / 健康指标 / 最近一次风险评估）" }
}
```

> 四轨数据优先从库里读（租户过滤）：第一轨按**设备类型**过滤指标（见 `device_types`），
> 查不到的数据**记为「证据缺失」**（`evidence_gaps` / `evidence_quality`）交给研判，
> 不再生成模拟读数。结果落库 `alerts`，`source=本服务`。

### 1b. POST /api/v1/alert/ingest — 接收业务系统 的报警记录（下行接入）

角色：caregiver / supervisor / admin。业务系统 才是报警的权威源：业务系统 的
业务系统的报警记录接口下发一条报警，本服务 研判后把处置结果经
发件箱回传（payload 带 `external_alert_id`），由业务系统回写处置结果，形成双向闭环。

**请求体**（字段对齐 报警记录）

```json
{
  "external_alert_id": "W-20261013-001",
  "device_id": "dev-001",
  "site_id": "e001",
  "device_type": "智能床垫",
  "alert_content": "检测到电流过载 320A",
  "location": "3号楼201-2床",
  "alert_time": "2026-10-13 16:00:00",
  "alert_type": ""
}
```

`device_type` 支持中文名或编码；`alert_type` 留空时按设备类型 + `alert_content`
关键词推断（如"跌倒"→`fall_detected`）。

**响应 200**

```json
{
  "alert_id": "alert-xxxxxxxxxxxx",
  "external_alert_id": "W-20261013-001",
  "alert_type": "current_abnormal",
  "source": "saas",
  "status": "awaiting_review",
  "review_data": { "...": "风险评估 + 四轨 + device_profile + evidence_quality" }
}
```

**幂等**：同一 `(tenant_id, external_alert_id)` 只研判一次；重复下发返回
`{"duplicated": true, "alert_id": "<既有告警>"}`，不会重复派单。

### 2. GET /api/v1/alert/{alert_id}/review — 获取审核数据

角色：仅 supervisor / admin。直读 `alerts` 表（租户隔离，跨租户 404）。

**响应 200**

```json
{
  "alert_id": "...", "site_id": "...", "device_id": "...",
  "alert_type": "...", "status": "...", "risk_level": "high",
  "risk_assessment": {},
  "device_readings": [], "service_records": [], "health_indicators": [],
  "device_profile": { "type_code": "mattress", "type_name": "智能床垫", "trust_level": 4 },
  "evidence_quality": "partial", "evidence_gaps": ["设备读数", "健康指标"],
  "source": "saas", "external_alert_id": "W-20261013-001", "location": "3号楼201-2床",
  "handled_by": "", "handled_at": null,
  "created_at": "...", "message": "请确认风险评估并派发工单"
}
```

### 3. POST /api/v1/alert/{alert_id}/confirm — 值班主管确认

角色：仅 supervisor / admin。恢复 interrupt 的图执行并派发工单（落库 `dispatch_orders`）；派单后落一条**出站事件**（`action=approve/modify` → `alert.dispatched`，`action=dismiss` → `alert.dismissed`）交给投递器回调 业务系统，见第六章「出站通知」。

**请求体**

```json
{
  "action": "approve",
  "modified_actions": [],
  "supervisor_id": "u-supervisor",
  "comment": ""
}
```

`action` 取值：`approve`（按建议派发）/ `modify`（按 `modified_actions` 修改后派发）/ `dismiss`（驳回）。

**响应 200**

```json
{
  "alert_id": "...",
  "status": "dispatched",
  "dispatch_order": {},
  "structured_output": {}
}
```

---

## 六、作业流程 Agent（状态机）

### POST /api/v1/work-flow/start

启动作业流程会话（阶段：建档评估→日常巡检→阶段复评→退场评估；异常处置为日常巡检内子模式，由关键词触发切换）。

**请求体**

```json
{
  "site_id": "elder-001",
  "service_plan_id": "",
  "initial_stage": "onboarding_assessment",
  "discharge_order": null
}
```

- `discharge_order`：可选，**退场工单**（dict）——业务系统（业务系统）下发的"该点位办理退住/退场"指令，即"该点位确定要退场"这一业务事件的信号载体；状态机只判"有无"，不读内容。示例：

  ```json
  { "order_id": "DO-20261010-001", "reason": "家属申请接回居家服务", "issued_by": "saas" }
  ```

  建档时若已确定要退场，可一并下发，作为 state 字段贯穿整场会话；收到工单后进入"收敛模式"——当前阶段达到最少轮数即收敛到阶段复评 → 退场评估主干道（不再开日常巡检新循环）。`null`/省略 = 续住。

**响应 200**：`{session_id, current_stage, ai_message, structured_output}`；同时在 `work_flow_sessions` 登记会话（业务侧权威记录，供"继续未完成流程"查询）。报告只在流程走到 `finished` 时才由 `save_report` 节点落库 `work_flow_reports`（阶段复评不会触发——续住回日常巡检、走退场评估都要继续对话）。

### GET /api/v1/work-flow/active/{site_id}

按点位查最近一场**进行中**的作业流程会话（租户隔离，无则 `active=false`）。

### GET /api/v1/work-flow/sessions/{session_id}/plan-overview — 服务计划总览

前端「服务计划」页的数据源：风险等级 + 服务项 + 当天计划与执行进度，**一个接口给足**。
不跑 LangGraph、不调 LLM（只读 Provider），所以很快。

**响应 200**

```json
{
  "session_id": "cf-xxxx",
  "site_id": "e001",
  "plan_missing": false,
  "plan_source": "merged",
  "care_level": "介助",
  "special_notes": "高血压，需监测血压",
  "service_items": [
    { "project_name": "设备状态检查", "frequency_name": "每2小时", "times_per_day": 6,
      "nurse_time_slots": [{ "startTime": "08:00", "endTime": "20:00" }],
      "is_required": true, "care_level": "介助" }
  ],
  "today_plan": [
    { "plan_item_id": "dcp-2026-10-13-001", "project_name": "环境检查",
      "nurse_time": "07:00-08:00", "status": "done",
      "carer_name": "张三", "actual_work_time": "08:05" }
  ],
  "progress": { "total": 16, "done": 1, "missed": 1, "pending": 14 },
  "message": ""
}
```

- `plan_source`：`service_items`（业务配置服务项）/ `ai_assessment`（评估结论派生）/
  `merged`（两者）/ `pipeline`（评估流水线注入）/ `missing`。**界面要如实展示来源**，
  让使用者知道任务清单是谁定的。
- `plan_missing=true` 时：`service_items` / `today_plan` 为空、`message` 给出引导
  （"先完成风险评估或登记服务项"），前端应展示引导而不是空表格。
- 会话不存在或跨租户 → 404。

### POST /api/v1/work-flow/plan-items/{plan_item_id}/confirm — 执行回写

护工确认某项服务任务执行完成（对应业务系统侧的执行结果回写接口）。

**请求体**

```json
{ "status": "done", "carer_name": "张三", "actual_work_time": "08:05" }
```

- `status` 只接受 `done`（完成）/ `missed`（该做没做），其它值 → 400。
- `missed` 会进入服务历史的异常记录，供后续对话与阶段复评参考。
- 回写的是**执行结果**（谁在什么时候做了/没做）这类事实数据；服务项目与频次等业务规则
  仍由业务侧决定，本服务 不改——两种 Provider 的落点不同（本地写 本服务 自己的
  `daily_inspection_plans`，业务系统 实现调主系统接口）。

**响应 200**：`{active: true, session: {session_id, site_id, staff_id, current_stage, total_turns, started_at, updated_at}}`。

用途：作业人员从点位详情页进入时，前端先调这里；有进行中的会话就提示"继续原流程"，拿回 session_id 进入对话页即可命中检查点续跑，不依赖浏览器记住 URL。

### POST /api/v1/work-flow/chat

阶段内对话（SSE 流式）。**请求体**：

```json
{
  "session_id": "cf-xxxx",
  "message": "点位今天翻身正常",
  "discharge_order": null
}
```

- `discharge_order`：可选，**退场工单**（dict，含义同 `/start` 处说明——业务系统 下发的"该点位确定要退场"业务事件信号），可**每轮重新下发**，模拟 业务系统 任何时点推送。一旦送入即落进 checkpointer 跨轮持久，收到后进入"收敛模式"：当前阶段达到最少轮数即收敛到 复评 → 退场评估 主干道（异常子模式内暂缓）。要撤销已下发的工单需开个新会话（同 elder 重新 start）。

服务端先按 `session_id` 从 `work_flow_sessions` 找回 `site_id` 再拼 thread_id（保证与 `/start` 写入的检查点键一致）；每轮结束后把 `current_stage` / `total_turns` 同步回登记表，走到 `finished` 自动把会话置为完成。

**SSE 事件序列**：`session` → `message` →（`report`）→ `done`

| 事件 | data 字段 |
|---|---|
| `session` | `{session_id}` |
| `message` | `{content, current_stage, daily_inspection_mode, total_turn_count}` |
| `report` | 流程到 `finished` 阶段时推送完整服务报告 |
| `error` / `done` | 同通用约定 |

### POST /api/v1/work-flow/sessions/{session_id}/discharge-order

业务系统 **主动下发**退场工单到指定会话（无需作业人员发言）。与 MCP 工具 `/mcp/本服务-flow` 的 `send_discharge_order` 共用同一段落地逻辑（`backend/agents/work_flow/discharge.py`）。

**请求体**：

```json
{ "order": { "order_type": "discharge", "reason": "转院" } }
```

只更新该会话检查点里的 `state.discharge_order`（**不推进对话、不耗 LLM、不生成 AI 回复**）：作业人员下一次 `/chat` 时由 `check_stage_node` 识别，当前阶段达 min 即收敛到 复评 → 退场评估。

**响应 200**：`{found, session_id, site_id, current_stage, status: "delivered", message}`；会话不存在 / 跨租户 / 已结束 → `404`。

### 出站通知（Agent → 业务系统）：回调配置与事件审计

这是全项目统一的**下行通道**：Agent 链路的终态结果主动 POST 回 业务系统，而不是让业务系统轮询。当前接入两个来源（共用一张发件箱表）：

| 来源 | 事件类型 | 幂等键 | 触发点 |
|---|---|---|---|
| 作业流程 | `work_flow.finished` | `session_id` | `save_report` 节点（报告落库后） |
| 告警研判 | `alert.dispatched` / `alert.dismissed` | `order_id` | `dispatch_order` 节点（值班主管确认后） |

投递走**发件箱模式**：事件先落 `outbound_events`，由后台投递器异步 POST，失败按指数退避重试。

`work_flow.finished` 载荷示例：

```json
{
  "event": "work_flow.finished",
  "session_id": "cf-xxxx",
  "site_id": "e001",
  "service_plan_id": "plan-1",
  "stage_reached": "finished",
  "total_turns": 18,
  "discharge_driven": true,
  "report": { "overall_score": 88, "dimensions": [], "care_suggestions": [] },
  "report_url": "/api/v1/work-flow/cf-xxxx/report",
  "occurred_at": "2026-10-12T10:30:00+00:00"
}
```

`alert.dispatched` 载荷示例：

```json
{
  "event": "alert.dispatched",
  "alert_id": "alert-xxxx",
  "site_id": "e001",
  "order_id": "order-xxxx",
  "device_id": "dev-001",
  "alert_type": "fall",
  "risk_level": "high",
  "actions": ["立即断电检查", "通知使用方", "2 小时后复测"],
  "order_content": "风险等级：high\n处置措施：...",
  "supervisor_id": "sup-001",
  "status": "dispatched",
  "occurred_at": "2026-10-12T10:30:00+00:00"
}
```

投递请求头：

| 头 | 说明 |
|---|---|
| `X-CareAgent-Event` | 事件类型，如 `work_flow.finished` |
| `X-CareAgent-Event-Id` | 事件 ID，**消费端用它做幂等去重**（投递语义是「至少一次」） |
| `X-CareAgent-Delivery-Attempt` | 这是第几次投递 |
| `X-CareAgent-Timestamp` | 发送时间（Unix 秒，供消费端防重放） |
| `X-CareAgent-Signature` | `sha256=<HMAC-SHA256(secret, body)>`，`secret` 未配置时不下发该头 |

投递与重试语义：2xx 视为成功；失败按 30s → 120s → 600s → 1800s 退避重试，第 5 次仍失败置 `failed`（保留记录、可人工重投）。租户未配置回调地址或 `enabled=false` 时，事件保留 `pending` 且**不计尝试次数**，配置好后自动补投。闭环链路不阻塞业务：投递发生在流程之外的后台循环里。

### PUT /api/v1/work-flow/webhooks

登记 / 更新本租户回调地址（**需 supervisor / admin 角色**）。

```json
{ "url": "https://saas.example.com/hooks/本服务-agent", "secret": "hmac-key", "enabled": true }
```

`url` 必须是 `http(s)://` 开头。只写配置、不做连通性校验（真正的投递由后台投递器异步完成）。

### GET /api/v1/work-flow/webhooks

查看本租户回调配置，密钥打码只回显前缀。未配置返回 `{"configured": false, "webhook": null}`。

### GET /api/v1/work-flow/events?limit=50

列出本租户出站事件（最近优先，`limit` 上限 200）。每条含 `status`（pending / sent / failed）、`attempts`、`last_error`、`next_retry_at`、`sent_at`，用于排障与对账。

### POST /api/v1/work-flow/events/{event_id}/redeliver

把失败 / 已发事件重置为 `pending` 交给投递器重发（人工兜底，**需 supervisor / admin 角色**）。事件不存在或跨租户 → `404`。

### GET /api/v1/work-flow/{session_id}/report

查询服务评估报告（直读 `work_flow_reports`，租户隔离）。响应含 `report`（完整报告）、`stage_reached`、`total_turns` 等。

### GET /api/v1/work-flow/{session_id}/report/export

把作业流程报告导出为 **Word（.docx）**：标题 → 基本信息表（会话编号/点位编号/到达阶段/总轮数）→ 报告正文 → 明细 → 免责声明。租户隔离同上。

---

## 七、统一对话入口（SSE）

### POST /api/v1/chat

由 Orchestrator 意图识别自动路由到 4 个 Agent，支持显式指定与 Pipeline 串联。

**请求体**

```json
{
  "query": "帮点位李四做一次跌倒风险评估",
  "site_id": "elder-001",
  "session_id": "",
  "agent_type": null,
  "mode": "single",
  "pipeline": null
}
```

- `agent_type`：`assessment` / `sop_qa` / `alert` / `work_flow`；`null` = LLM 意图识别自动路由
- `mode`：`single`（单 Agent） / `pipeline`（需同时传 `pipeline`，如 `care_workflow`：评估→作业流程）

**SSE 事件序列**：`session` → (`clarify` | `answer`) → `done`

| 事件 | data 字段 |
|---|---|
| `clarify` | `{candidates[], message, reasoning, query}` — 意图跨域无法判定时返回候选，前端引导用户二选一 |
| `answer` | `{content, agent_type, mode, structured_output, fallback_used}` |

### DELETE /api/v1/chat/session/{site_id}/{session_id}

删除整个会话线程的检查点：该 `thread_id` 下的 **所有** `checkpoints` / `checkpoint_blobs` / `checkpoint_writes`
一次清空，包含各 Agent 命名空间与**暂停中的 interrupt 断点**（告警等待值班主管确认的状态也在内）。

什么时候用：确认这个服务周期已彻底结束，且审计期已过，不再需要任何会话状态。
什么时候别用：只是想重新开始一轮对话——换个 `session_id` 即可，不要删历史；删除不可恢复。

**权限**：仅 `admin`（破坏性操作）；**租户**：点位必须归属当前租户，跨租户按不存在处理。

| 项 | 说明 |
|---|---|
| 路径参数 | `site_id`、`session_id`（会话标识，告警场景即 `alert_id`） |
| 成功响应 | `{thread_id, deleted, backend, message}` |
| 没跑过的会话 | `deleted=false`，不报错（幂等） |
| 典型错误码 | `401` 未带令牌 / `403` 非 admin / `404` 点位不存在或跨租户 |

```bash
curl -X DELETE http://localhost:8000/api/v1/chat/session/e001/sess-abc \
  -H "Authorization: Bearer <admin_token>"
# → {"thread_id":"elder_e001_session_sess-abc","deleted":true,"backend":"postgres",
#    "message":"已删除该会话线程的全部检查点"}
```

底层就是 checkpointer 的 `adelete_thread(thread_id)`：实现见 `backend/core/memory.py` 的 `delete_thread_state`，
后端是 PostgreSQL 时走官方原子删除，回落内存时逐个 Agent 实例清理。

**没有自动清理**：本版本不接 TTL、不做定时任务、也不在业务节点里挂钩子——检查点会一直保留，直到有人显式调这个接口。
原因是删除不可恢复且按 thread 整体生效，判定必须结合业务状态，不适合交给一个通用定时规则。
要批量保留策略时，按"业务状态（报告已生成 / 告警已 dispatched）+ 该 thread 最后更新时间"生成待删清单，
建议先 dry-run 打印清单、确认后再执行。

---

## 八、作业规范知识库检索（语义搜索）

### POST /api/v1/knowledge/search

作业规范 语义搜索：**混合召回（稠密 + 稀疏）→ 重排精排 → 带置信度返回**。
与 SOP 问答 Agent 的检索节点共用同一实现（`cached_retrieve`），因此同样享受 Redis 检索缓存与租户强隔离。

**请求**

```json
{
  "query": "配电间喂养前要检查什么",
  "category": "feeding_tube",
  "top_k": 3,
  "recall_top_k": 10,
  "min_confidence": 0.0
}
```

| 字段 | 必填 | 说明 |
|---|---|---|
| `query` | 是 | 作业问题（中文或英文） |
| `category` | 否 | SOP 类别 ID（如 `feeding_tube`），用于缩小检索范围 |
| `top_k` | 否 | 精排后返回条数，默认 3，上限 20 |
| `recall_top_k` | 否 | 混合召回条数，默认 10，上限 50 |
| `min_confidence` | 否 | 过滤低于该分数的结果（0–1） |

**响应**

```json
{
  "query": "配电间喂养前要检查什么",
  "category": "feeding_tube",
  "count": 2,
  "confidence": 0.9976,
  "is_high_confidence": true,
  "hits": [
    {
      "content": "…喂养前必须检查管路位置、胃残留量…",
      "source_name": "feeding_tube > 配电间巡检规范（SOP） > 4. 日常维护操作步骤",
      "category": "feeding_tube",
      "score": 0.998
    }
  ]
}
```

**性能**：首次调用需加载嵌入与精排模型（含冷启动约 10 秒），模型就绪后单次检索约 2-4 秒；
同一问题二次调用命中 Redis 缓存，实测从 **2.01 秒降到 0.008 秒**（本机，检索段，不含回答生成）。
检索链路异常时返回 `503`，不返回 500（与 Agent 链路的降级策略一致）。

```bash
curl -X POST http://localhost:8000/api/v1/knowledge/search \
  -H "Authorization: Bearer <token>" -H "Content-Type: application/json" \
  -d '{"query":"配电间喂养前要检查什么","top_k":2}'
```

> 对应的 MCP 工具是 `sop-kb` Server 的 `search_sop`（见 [MCP 文档](mcp.md)、[工具清单](工具清单.md)）。

---

## 九、IoT 设备接入

### POST /api/v1/devices/{device_id}/readings

设备网关上报读数（支持批量），存库 `device_readings` 供告警四轨读取。

**请求体**

```json
{
  "site_id": "elder-001",
  "device_type": "配电监测",
  "readings": [
    {
      "metric_name": "current",
      "reading_value": "320",
      "reading_unit": "A",
      "is_abnormal": true,
      "recorded_at": "2026-09-18T10:30:00"
    }
  ]
}
```

**响应 200**：`{"device_id": "...", "saved": 3}`；点位跨租户 → 403；点位/设备不存在时自动补占位行（归属当前租户）。

### GET /api/v1/devices/elder/{site_id}/readings?limit=10

查询点位最近设备读数（租户隔离，跨租户返回空列表）。响应：`{"readings": [...]}`。

---

## 十、健康检查

### GET /health

无需认证。响应：`{"status": "ok", "service": "CareAgent", "version": "1.0.0"}`

---

## 十一、curl 快速示例

```bash
# 登录
TOKEN=$(curl -s -X POST http://localhost:8000/api/v1/auth/login \
  -H "Content-Type: application/json" \
  -d '{"username":"caregiver","password":"Caregiver@123"}' | python -c "import sys,json;print(json.load(sys.stdin)['access_token'])")

# SOP 问答（SSE）
curl -N -X POST http://localhost:8000/api/v1/sop-qa/chat \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"query":"设备温升预防的作业要点有哪些？"}'

# IoT 上报读数
curl -X POST http://localhost:8000/api/v1/devices/device-hr-01/readings \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"site_id":"elder-001","device_type":"配电监测","readings":[{"metric_name":"current","reading_value":"320","reading_unit":"A","is_abnormal":true}]}'

# 触发告警（caregiver 可）
curl -X POST http://localhost:8000/api/v1/alert/trigger \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"site_id":"elder-001","device_id":"device-hr-01","alert_type":"current_abnormal"}'
```
