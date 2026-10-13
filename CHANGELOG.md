# Changelog

遵循 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/) 与语义化版本。

## [Unreleased]

### Added

- **设备谱系**（`backend/core/device_types.py` + `device_types` 表）：9 类设备对齐机构 SaaS 的设备字典，
  每类登记「可上报指标 / 可产生的告警 / 证据可信度（1-5）与理由」；类型解析优先级为
  「报警自带声明 > 设备登记表 > 按告警类型反查」，声明与登记不一致时采信声明并留痕。
- **告警双向闭环**：`POST /api/v1/alert/ingest` 接收机构 SaaS 的报警记录（按
  `(tenant_id, external_alert_id)` 幂等），处置终态经发件箱回调并带回 `external_alert_id`，供业务侧回写处置结果。
- **照护计划 Provider 抽象**（`backend/core/care_plan_provider.py`）：`local`（care 自带库）与
  `saas`（对接机构业务系统）两种实现，职责边界显式——本地实现写 care 自己的库，
  SaaS 实现只读「该等级允许做什么」、落地由主系统负责。
- **评估结论驱动业务配置**：护理等级 → 该等级必选服务项 → 老人照护服务项 →
  照护任务清单（`care_level_configs` / `elder_service_items`）。
- **每日照护计划**（`daily_care_plans`）：按服务项 × 频次展开当天条目（单窗口多频次自动均分），
  支持执行回写（`POST /api/v1/care-flow/plan-items/{id}/confirm`，`done` / `missed`），
  漏做进入照护历史的异常记录。
- **照护计划总览接口与前端页面**（`GET /care-flow/sessions/{sid}/plan-overview`）：
  展示护理等级、服务项、当天执行进度与**计划来源**；计划缺失时给引导而不是空表格。
- **MCP 工具 4 个**：`list_elder_service_items` / `list_care_level_configs` /
  `get_today_care_plan`（只读，care-data）与 `confirm_care_plan_item`
  （写，care-flow，需 Token 与租户白名单）。工具总数 20 → 24。
- **依赖探活**：`/health/ready` 真探 PostgreSQL / Redis / Milvus（必需依赖不可用返回 503）；
  原 `/health` 保留为存活探针。
- **登录失败限流**：按「用户名 + 来源 IP」计数，窗口内失败达阈值后临时锁定
  （`429` 与 `Retry-After`）。
- **建号脚本** `scripts/create_user.py`（演示账号默认不再播种）。
- **公开仓库导出** `scripts/export_public.sh`（白名单导出与泄漏扫描）。

### Changed

- **对外标识符去个人化**：`waring_id` 改名为 `external_alert_id`（前者是从源系统照搬的
  `warning` 拼写错误，出现在公开 API 上不合适）；涉及 REST 字段、数据库列与唯一索引、
  出站事件 payload 与文档。旧库里的 `waring_id` 列不会被自动删除，可手工清理。
- **业务系统端点改为具名占位常量**：`SaasCarePlanProvider.PATH_*` 四个路径不再把某个具体
  机构的接口路径写死在调用处，注释里说明"按你自己的后端契约调整"；相关注释与文档中的
  公司系统端点一并泛化。
- **告警四轨采集不再生成模拟数据**。原来设备读数 / 照护记录 / 健康指标在库里查不到时会现场编造
  硬编码读数（心率永远 135、离床永远 15 分钟），导致风险等级由常量决定。现在查不到就记为
  「证据缺失」（`evidence_gaps` / `evidence_quality`）显式交给研判，并在提示词中加入证据纪律：
  完整性不足时不得判低风险、低可信设备单源不足以定性。
- **照护计划不再用 mock 兜底**。取不到计划时标记 `care_plan_missing` 并由 `notify_missing_plan`
  节点如实告知；照护历史改读真实记录（`daily_care_plans` 与 `care_records`），没有就是空列表。
- **任务清单来源改回业务配置**：`_build_care_plan_from_assessment` 不再用评估的 issue 拼
  `daily_tasks`，改为来自该护理等级绑定的服务项——修正了「AI 决定该做什么服务」的越界。
- **CORS 默认值**：修正通配来源搭配凭证这一浏览器会拒绝的无效组合；默认通配来源且关闭凭证
  （Bearer Token 鉴权不需要 Cookie），并支持白名单配置。
- **默认账号播种**：`SEED_DEMO_USERS` 默认 `false`，不再一启动就带人人皆知的默认口令。

### Fixed

- **文档数字与代码对齐**（发布前自查）：REST/SSE 接口 26 → **27**、前端页面 10 → **11**（含登录页）；
  完整项目文档里遗留的 149 项测试 / 30 节点 / 9 张表 / 8,190 行等旧数字一并更正为
  239 项 / 31 节点 / 16 张表 / 后端 12,330 行。
- **MCP 工具 schema 重新生成**：`文档/mcp_tools_schema.json` 此前只有 16 个工具
  （`dump_mcp_tools.py` 漏配 care-flow Server 导致），现为 6 Server / 24 工具。
- **数据来源声明**：新增 `data/README.md`，说明 `data/sop/` 下 6 篇 SOP 是示例内容、
  不是临床规范，以及替换知识库后需要重建的索引与回归集。
- **测试套件对本机环境的隐藏依赖**：`test_summary_survives_next_turn_within_same_thread` 里
  `monkeypatch.setattr(sop_nodes, "classify_query_node", ...)` 是无效补丁——
  `graph.py` 在模块顶层 import 了该函数，图上挂的是它自己模块的绑定，补丁改不到。
  结果是这个自称"闲聊分支、不检索"的用例实际走进了检索链路：本机有模型权重时只慢一秒，
  没有权重时会去 HuggingFace 下载约 2GB（在无权重的副本里实测 150 秒起步）。
  现已改为打图模块的名字并加检索兜底；同时新增 `models_ready` 夹具，
  让真正需要权重的用例在缺权重时**跳过而不是默默下载**。
- `test_web_search` 的两个用例用 `setattr(func, "__call__", ...)` 给 settings 打补丁，
  对普通函数无效——它们此前只在本机恰好配了 `TAVILY_API_KEY` 时通过。现改为替换模块属性，
  未配 Tavily Key 的环境同样通过。
- `scripts/dump_mcp_tools.py` 漏配 `care-flow` Server、默认输出路径仍指向已改名的 `docs/`。
- `quick_index` 注释与行为漂移：注释举的例子实际被覆盖率门控拦下，与它想说明的机制不符。
- 文档中指向 `docs/` 的失效链接（目录已改名 `文档/`）、测试用例数与工具数的多处不一致。

### 说明

- 本次改造把「AI 判断」与「业务规则」的边界落到了代码与测试里，而不只是文档里：
  业务服务项覆盖 AI 生成的任务清单、缺证据不得判低风险、MCP 只读业务配置——均有对应用例守着。
- 公开 README 按「第一次看到这个仓库的人」的视角重写了快速开始：补上前置条件表
  （Python 版本硬要求、内存、磁盘、LLM 费用）、四个必填配置项、"首次启动慢且消耗额度"的
  预期管理，以及一条"跑起来之后先做什么"的最小闭环路径。同时修正了遗留的用例数。
- 新增 `scripts/capture_screenshots.mjs`（playwright 自动截图，**未在真实环境验证**）
  与 `docs/images/README.md`（说明需要哪几张图、每张要体现什么；图未就绪前 README 里的
  图片引用保持注释，避免坏图标）。
- 当前未做：每日计划的生成规则在生产形态应由业务侧负责（care 侧仅最小实现）、
  可观测性指标聚合、lite 模式（免模型免 Key 起步）。

## [1.0.0] - 2026-09

- 初始版本：4 个 LangGraph Agent + Orchestrator、MCP 能力层、多租户与 JWT、
  出站通知（发件箱）、Vue3 前端。
