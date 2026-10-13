# FieldOps · 现场作业 AI 多智能体平台

<!-- 发布前把 <owner>/<repo> 换成你的仓库路径，CI 徽章才会亮 -->
[![CI](https://github.com/<owner>/<repo>/actions/workflows/ci.yml/badge.svg)](https://github.com/<owner>/<repo>/actions/workflows/ci.yml)
![License](https://img.shields.io/badge/license-Apache--2.0-blue)
![Python](https://img.shields.io/badge/python-3.11%2B-blue)
![Frontend](https://img.shields.io/badge/frontend-Vue3%20%2B%20TS-42b883)

基于 LangGraph 的现场服务作业参考实现：把园区/物业运维里最耗人力的四条链路
（点位风险评级 / 作业规范问答 / 设备告警研判 / 作业流程执行）做成**可审计的 AI 流程**，
并明确划出「AI 能决定什么、必须由人拍板什么」。

> 这是一个**技术参考实现**，不是可直接上生产的安全管控系统。真实部署需要的合规与安全工作见文末。

<!-- 截图就绪后取消注释（先按 docs/images/README.md 生成图，否则会显示坏图标）：
![服务计划页](docs/images/service-plan.png)
-->

---

## 一、它解决什么问题

现场运维里有两类事：**判断类**（这个点位现在什么状况、这条告警要不要派人）和
**执行类**（今天该给谁做什么、做了没有）。这个项目的立场很明确——
AI 负责判断与提示，业务规则与最终决定权留给人。

### 业务闭环：风险等级 → 服务项 → 每日计划 → 执行回写

```
点位风险评级（AI）      评估表 → 5 维度并行评分 → 风险等级建议（一级/二级/三级）
      │
      ▼
风险等级 ──▶ 该等级要求的服务项目（业务配置，AI 不得增删）
      │
      ▼
点位服务项 ──▶ 每日作业计划（项目 × 频次 × 作业时间窗展开）
      │
      ▼
作业人员执行 ──▶ 回写结果（完成 / 未完成 + 执行人 + 实际时间）
      │
      ▼
漏做进服务历史的异常记录 ──▶ 下一轮对话与阶段复评的依据
```

### 告警闭环：AI 研判，值班主管决断

```
业务系统报警记录 ──POST /api/v1/alert/ingest──▶ 四轨证据采集 ──▶ LLM 风险研判
   (带 external_alert_id)                       │
                                                ▼
                                 interrupt() 硬暂停，等值班主管 approve/modify/dismiss
                                                │
                                                ▼
                             派发工单 ──▶ 发件箱（outbox）──▶ 回调业务系统回写处置
```

## 二、四个关键设计（这个项目的重点在这里）

**1. AI 不越权做业务决策。** 服务项目、频次、作业时间段来自机构业务配置——
接口和 MCP 工具都只让外部系统**读**这些，改不了。AI 的增量被限定在：
评级结论、从可选集合里推荐、异常处置建议、执行质量核查。这条边界在代码里是可验证的
（`tests/test_service_tier_binding.py` 断言"业务服务项覆盖 AI 生成的任务清单"）。

**2. 责任边界落成 breakpoint。** 涉及人身安全的场景 AI 不能替人拍板：告警链路在风险评估后
用 LangGraph `interrupt()` 硬暂停，值班主管看到四轨证据后确认，才恢复执行派单。
AI 的建议与人的决定分别留痕。

**3. 证据纪律：缺证据不等于没异常。** 四轨数据（设备读数 / 服务记录 / 运行参数 /
最近一次风险评估）任何一轨查不到数据，都记成「证据缺失」显式交给研判——**不生成模拟读数**。
提示词里写明：完整性不足时不得判低风险；证据可信度 ≤2 的设备单源不足以定性。
设备类型本身带可信度（直接测电气参数的仪表 vs 人流量统计不是一个量级），
研判按来源加权而不是四轨等权。

**4. 跨系统集成用发件箱，不用同步回调。** 业务终态先落库再投递：
HMAC-SHA256 签名、事件唯一键幂等、指数退避重试、投递审计与人工重投。
外部系统抖动不该拖垮作业流程收尾。告警是双向的：业务系统下发报警、本服务回写处置。

## 三、架构

```
前端层  Vue3 + TS + Element Plus（11 个页面，SSE 消费）
API 层  FastAPI + SSE（27 个 REST/SSE 接口，JWT + 角色门控 + 多租户强制隔离）
MCP 层  6 个 FastMCP Server / 24 个工具（只读免鉴权；写工具 Bearer Token + 租户白名单）
编排层  Orchestrator（意图路由 / 单 Agent 直达 / Pipeline 串联）
Agent   4 个 LangGraph StateGraph（31 个节点）
公共层  容错 / 缓存 / 记忆 / 检索 / 发件箱 / 设备谱系 / 服务计划 Provider
数据层  PostgreSQL（业务 + 检查点）、Milvus（向量）、Redis（缓存）、MinIO/etcd
```

| Agent | 模式 | 说明 |
|---|---|---|
| 点位评估 | 并行评审 | 上传评估表 → 5 维度（结构安全/设备状态/环境风险/作业风险/合规性）`asyncio.gather` 并发评分 → 加权汇总 → 等级建议 |
| 规范问答 | 快慢分层 | 快通道（术语规范化 + BM25 + 三重门控 + 诊断类问句门控）命中直接返规范原文；未命中走 HyDE/多查询 + 混合检索 + 精排 |
| 告警研判 | Human-in-the-Loop | 四轨并行采集 → 结构化风险研判 → `interrupt()` 等值班主管确认 → 派工单 |
| 作业流程 | 状态机 | 建档评估 → 日常巡检（含异常处置子模式）→ 阶段复评 → 退场评估；`AsyncPostgresSaver` 跨轮记忆 |

## 四、快速开始

**前置条件**（先确认这几项，缺一个都跑不起来）：

| 项 | 要求 | 说明 |
|---|---|---|
| Python | 3.11+ | 硬要求：LangGraph 的 `interrupt()` 在 3.10 上不可用，告警的人工确认会失效 |
| Docker | Docker Desktop + Compose v2 | 五个基础设施容器：PostgreSQL / Redis / Milvus / etcd / MinIO |
| 内存 | 建议 8 GB 以上可用 | Milvus + etcd + MinIO 同时跑比较吃内存 |
| 磁盘 | ≥10 GB | 模型权重 4.2 GB + 容器镜像 + 向量库数据 |
| 大模型 | DeepSeek 或 Qwen（DashScope）任一 API Key | 会**产生调用费用**；只配一个通道即可 |

```bash
# 1) 准备配置：cp 之后**必须填 4 个必填项**，否则启动即报错
cp .env.example .env.local
#    DB_USER / DB_PASSWORD      —— 与 docker-compose 传给 postgres 的保持一致
#    JWT_SECRET_KEY             —— 任意强随机串
#    DEEPSEEK_API_KEY           —— 或 QWEN_API_KEY（按 DEFAULT_LLM_PROVIDER 选一个）

# 2) 起基础设施 + 后端（首次会自动建表、导入种子数据与示例规范库）
./scripts/start.sh

# 3) 建一个登录账号（演示账号默认不播种，见下）
python scripts/create_user.py --username admin --role admin

# 4) 验证依赖是否就绪
curl http://localhost:8000/health/ready   # 就绪探针：真探 PostgreSQL/Redis/Milvus
```

前端另开一个终端：`cd frontend && npm install && npm run dev`（http://localhost:3000）。

> **首次启动会比较久，这是正常的**：规范库导入走 Contextual RAG，会逐块调用大模型
> （**消耗额度**）；首次检索请求还要加载 BGE 模型（本地权重约 1.5 秒，走在线拉取则要下载几个 GB）。

### 跑起来之后，先做这条最小路径

界面是空的，因为还没有数据。按顺序走一遍就能看到完整闭环：

1. **灌演示数据** → `python scripts/seed_data.py`（5 个点位 + 10 台设备 + 服务记录）；
2. **上传评估表**（前端「点位评估」页）→ 得到五维评分与风险等级建议；
3. **开始作业流程**（前端「作业流程」页）→ 进入「服务计划」页，能看到风险等级、服务项、
   今日计划与执行进度，逐项点「确认完成 / 未完成」；
4. **触发一条告警**（前端「告警审核」页，需 supervisor/admin）→ 看 AI 研判的四轨证据与
   风险等级，做值班主管确认；
5. **问一条作业规范**（「SOP 问答」页）→ 标准问法走快通道（毫秒级返回规范原文），
   其它问题走完整检索链路。

**关于模型权重**：规范问答的精排与嵌入依赖 `BGE-M3` 与 `BGE-Reranker`。
把权重放到 `backend/models/embedding/bge-m3/` 与 `backend/models/reranker/bge-reranker-large/`；
目录为空时会回退到从 HuggingFace 在线拉取（首次较慢）。权重不入库。

**关于登录**：演示账号**默认不播种**——开源项目不该一启动就带人人皆知的口令。
直接体验可在 `.env.local` 打开 `SEED_DEMO_USERS=true`；否则用 `scripts/create_user.py` 建自己的账号。

## 五、测试

```bash
docker compose --env-file .env.local up -d    # 起 PostgreSQL/Redis/Milvus
python -m pytest tests/ -q                    # 238 用例
```

没起基础设施时会自动跳过依赖数据库的用例（fail-soft），所以只跑
纯逻辑部分也能用（几秒跑完）——但提交前请确保在完整环境里是 **238 全绿**。

快通道另有一组回归（不依赖数据库与模型）：

```bash
python scripts/build_quick_index.py --no-llm   # 从示例规范重建索引
python scripts/evaluate_quick_channel.py       # 命中率 / 误命中率 / 判定耗时
```

口径：**误命中必须保持 0**——宁可回落 RAG 慢一点，不可给错章节。

## 六、配置要点

完整清单见 `.env.example`。几个与服务和安全相关的默认值：

| 变量 | 默认 | 说明 |
|---|---|---|
| `SEED_DEMO_USERS` | false | 是否播种演示账号 |
| `CORS_ORIGINS` | `*` | 来源白名单；留空 = 不启用 CORS（同源部署）。通配来源时后端会强制关闭凭证 |
| `LOGIN_MAX_ATTEMPTS` / `LOGIN_LOCKOUT_SECONDS` | 5 / 300 | 登录失败限流（按 用户名 + 来源 IP） |
| `MCP_ADMIN_TOKEN` | 空 | MCP 写工具的 Bearer Token；留空则写工具禁用（fail-closed） |
| `SERVICE_PLAN_PROVIDER` | local | 服务计划数据源：`local`（自带库）/ `saas`（对接机构业务系统） |
| `CHECKPOINT_BACKEND` | auto | 检查点后端；生产建议 `postgres`（连不上直接报错，避免静默丢状态） |

## 七、目录结构

```
fieldops-agent/
├── SCENARIO.md        示例场景与术语表（改动前先读它）
├── backend/
│   ├── agents/        4 个 LangGraph Agent（assessment / sop_qa / alert / work_flow）
│   ├── api/v1/        REST + SSE 接口
│   ├── core/          编排、容错、缓存、记忆、检索、发件箱、设备谱系、服务计划 Provider
│   ├── db/            数据访问与幂等迁移
│   └── mcp/           6 个 MCP Server
├── frontend/          Vue3 + TS + Element Plus SPA
├── data/sop/          作业规范示例（可替换为自有规范）
├── scripts/           一键启动 / 建号 / 建库 / 快通道索引与回归 / 延迟压测
└── tests/             pytest（238 用例）
```

## 八、合规、安全与免责声明

本项目处理**现场设备与作业数据**，真实部署前至少需要处理：

- **数据出域**：默认实现把评估文本、服务记录、告警证据等内容发给大模型服务商做推理。
  这意味着现场数据离开组织边界——真实部署前必须与服务商确认数据处理条款，
  或改用私有化部署的模型；对合规敏感的场景，建议只发送必要字段而不是整段原始记录。
- **人员信息**：若接入人员定位卡、门禁或视频行为分析，涉及员工与访客的个人信息
  （生物识别信息属敏感个人信息），需按《个人信息保护法》取得同意、做最小化采集，
  并在现场显著位置告知视频采集范围。
- **视频与影像**：视频行为分析的告警证据涉及影像留存，需明确留存期限与访问权限。
- **人工复核**：本项目把 AI 结论设计为"建议 + 人审"，但组织仍需按自身制度明确
  哪些动作必须由持证人员确认（尤其停电、动火、受限空间等作业）。
- **审计留痕**：处置链路（谁在什么时候确认了什么）应满足组织与监管的审计要求。
- **示例数据非真实数据**：仓库里的种子数据（点位、设备、服务记录）与 `data/sop/` 下的
  规范文档均为合成的示例内容，不含任何真实信息，也**不能当作现场作业标准**。

**免责声明**：本项目是技术参考实现，不构成安全管控系统或作业许可依据，
不对任何现场作业结果负责。请勿直接用于生产环境的安全决策。

## 九、依赖与许可

本项目以 **Apache-2.0** 发布（见 `LICENSE`）。主要第三方依赖与其许可：

| 依赖 | 许可 |
|---|---|
| LangChain / LangGraph | MIT |
| FastAPI / Starlette / Pydantic | MIT / BSD |
| Vue 3 / Element Plus | MIT |
| `BAAI/bge-m3`（嵌入） | MIT |
| `BAAI/bge-reranker-large` / `v2-m3`（精排） | MIT / Apache-2.0 |

`data/sop/` 下的规范文档为本项目自撰的示例内容，可自由替换为组织自有规范；
若替换为第三方资料，请自行确认授权。

## 十、贡献

欢迎 issue 与 PR。提交前请确保：

```bash
docker compose --env-file .env.local up -d
python -m pytest tests/ -q          # 238 全绿
cd frontend && npm run build        # 含 vue-tsc 类型检查
```

改动涉及工具或节点时，记得重跑 `python scripts/dump_mcp_tools.py` 更新工具 schema；
改动领域概念时，请同步更新 `SCENARIO.md` 的术语表——它是这个仓库所有命名与文案的依据。

## Roadmap

- **lite 模式**：不装模型权重、不配 LLM Key 也能跑起来看到界面（当前起步成本偏高）；
- 每日作业计划的生成规则在生产形态应由业务系统负责（本仓库只有最小实现）；
- 快通道的"适用范围"字段：让不同设备的同名判据（温度/水位/压力阈值）可区分，
  消除留出集里出现的那 1 条章节误命中；
- 可观测性：检索命中率 / 降级率 / P95 延迟的指标聚合。

---

## English Summary

**FieldOps** is a reference implementation of an AI-assisted field-service operations
platform, built on LangGraph. Four domain agents (site risk assessment, work-spec Q&A,
IoT alert triage, and a work-order state machine) sit behind an orchestrator, with MCP
tools for external integration and an outbox for reliable cross-system callbacks.

The design puts the responsibility boundary in code, not in prose: service items and
their frequency come from institutional configuration and the AI cannot add or modify
them; alert triage hard-pauses at a LangGraph `interrupt()` until a human supervisor
confirms; missing evidence is reported as missing instead of being filled with
simulated readings; and device evidence carries a trust level so weak sources cannot
decide on their own.

This is a technical reference, **not** a safety-management or work-permit system.
See the compliance section above before using it with real site data.
