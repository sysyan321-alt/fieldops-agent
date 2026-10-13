# backend/core/orchestrator.py
# Agent 编排器：路由请求、支持单 Agent 和 Pipeline 模式

from __future__ import annotations
from enum import Enum
from typing import Any, Optional
from pydantic import BaseModel, Field

from backend.core.logger import get_logger
from backend.core.retry import with_retry
from backend.core.memory import build_config

logger = get_logger(__name__)


# ──────────────────────────────────────────────────────────────
# 一、枚举与请求模型
# ──────────────────────────────────────────────────────────────

class AgentType(str, Enum):
    """支持的 Agent 类型。"""
    ASSESSMENT = "assessment"   # 老年人能力评估
    SOP_QA     = "sop_qa"       # 护理 SOP 问答
    ALERT      = "alert"        # 告警研判
    WORK_FLOW  = "work_flow"   # 照护流程


class ExecutionMode(str, Enum):
    """执行模式。"""
    SINGLE    = "single"    # 单 Agent 直达
    PIPELINE  = "pipeline"  # 多 Agent 流水线
    CLARIFY   = "clarify"   # 需要澄清（无法路由）


class AgentRequest(BaseModel):
    """统一的 Agent 请求模型。"""
    staff_id:   str               # 护理员/站长 ID
    site_id:   str = Field(default="")  # 老人 ID（评估/告警/照护流程用）
    tenant_id:  str = Field(default="")  # 租户 ID
    session_id: str               # 会话 ID
    agent_type: Optional[str] = None  # 指定 Agent（None = 由意图识别路由）
    query:      str = Field(default="")  # 护理员输入
    mode:       str = "single"    # single / pipeline
    pipeline:   Optional[str] = None  # Pipeline 名称
    context:    dict = Field(default_factory=dict)  # 附加上下文

    @property
    def thread_id(self) -> str:
        """构造 thread_id（护理员维度隔离）。"""
        return f"staff_{self.staff_id}_session_{self.session_id}"


# ──────────────────────────────────────────────────────────────
# 二、Pipeline 定义
# ──────────────────────────────────────────────────────────────

PIPELINES: dict[str, list[AgentType]] = {
    # 先评估再制定照护流程
    "service_workflow": [AgentType.ASSESSMENT, AgentType.WORK_FLOW],
}


# ──────────────────────────────────────────────────────────────
# 三、Orchestrator 主类
# ──────────────────────────────────────────────────────────────

class Orchestrator:
    """Agent 编排器（单例）。

    职责：
        1. 按 Agent 类型获取对应的编译后的图
        2. 单 Agent 模式：直接调用
        3. Pipeline 模式：按顺序调用多个 Agent，上下文传递
    """

    def __init__(self):
        self._graphs: dict[str, Any] = {}  # 懒加载缓存
        logger.info("orchestrator.initialized")

    # ── 图懒加载 ────────────────────────────────────────────

    async def _get_agent_graph(self, agent_type: str) -> Any:
        """懒加载并缓存 Agent 编译后的图。

        async 原因：带检查点的图构建要先就绪 AsyncPostgresSaver（建池 + 建表）。
        """
        if agent_type not in self._graphs:
            if agent_type == AgentType.ASSESSMENT.value:
                from backend.agents.assessment.graph import build_assessment_graph
                self._graphs[agent_type] = build_assessment_graph()
            elif agent_type == AgentType.SOP_QA.value:
                from backend.agents.sop_qa.graph import build_sop_qa_graph
                self._graphs[agent_type] = await build_sop_qa_graph()
            elif agent_type == AgentType.ALERT.value:
                from backend.agents.alert.graph import build_alert_graph
                self._graphs[agent_type] = await build_alert_graph()
            elif agent_type == AgentType.WORK_FLOW.value:
                from backend.agents.work_flow.graph import build_work_flow_graph
                self._graphs[agent_type] = await build_work_flow_graph()
            else:
                raise ValueError(f"未知 agent_type: {agent_type}")
            logger.info("orchestrator.graph_loaded", agent_type=agent_type)
        return self._graphs[agent_type]

    # ── 单 Agent 模式 ────────────────────────────────────────

    async def _run_single_agent(
        self, request: AgentRequest, agent_type: str,
    ) -> dict:
        """执行单个 Agent。"""
        graph = await self._get_agent_graph(agent_type)
        # agent_type 一并写进 config 的 checkpoint_ns：同一 thread_id 下各 Agent 的
        # 检查点互不覆盖（统一对话入口下，多个 Agent 会共用同一个 session_id）
        config = build_config(
            request.site_id or request.staff_id, request.session_id, agent_type=agent_type,
        )

        # 构造初始 State（各 Agent 需要的字段不同）
        state: dict = {
            "messages": [],
            "tenant_id": request.tenant_id,
            "site_id": request.site_id,
            "session_id": request.session_id,
        }

        # 各 Agent 特有字段
        if agent_type == AgentType.ASSESSMENT.value:
            state["assessment_id"] = request.context.get("assessment_id", "")
            state["pdf_local_path"] = request.context.get("pdf_local_path", "")
        elif agent_type == AgentType.SOP_QA.value:
            state["original_query"] = request.query
            state["org_id"] = request.context.get("org_id")
            state["enable_web_search"] = request.context.get("enable_web_search", False)
        elif agent_type == AgentType.ALERT.value:
            state["alert_id"] = request.context.get("alert_id", "")
            state["device_id"] = request.context.get("device_id", "")
            state["alert_type"] = request.context.get("alert_type", "heart_rate_abnormal")
            state["alert_timestamp"] = request.context.get("alert_timestamp", "")
            # 下行来源：机构 SaaS 下发的报警（报警记录）带 external_alert_id 与报警位置，
            # 处置完成后按 external_alert_id 回写 Handle报警记录；care 自触发时为 care/空
            state["source"] = request.context.get("source", "local")
            state["external_alert_id"] = request.context.get("external_alert_id", "")
            state["alert_location"] = request.context.get("alert_location", "")
            # 报警自带的设备类型声明（SaaS 的 报警记录 带"设备类型"）——
            # 它是"这台报警设备是干什么的"的直接声明，解析设备谱系时优先级高于本地登记
            state["device_type_declared"] = request.context.get("device_type", "")
        elif agent_type == AgentType.WORK_FLOW.value:
            state["service_plan_id"] = request.context.get("service_plan_id", "")
            state["current_stage"] = request.context.get("current_stage", "onboarding_assessment")
            # Pipeline 模式下，上游（如 assessment）传入的 service_plan 注入 state；
            # 单 Agent 直调时 service_plan 为 None，由 load_context 节点用 mock 兜底
            upstream_service_plan = request.context.get("service_plan")
            if upstream_service_plan:
                state["service_plan"] = upstream_service_plan
            # SaaS 注入的退住工单（None=续住）：阶段复评流转时据此决定出院 or 回日常巡检
            discharge_order = request.context.get("discharge_order")
            if discharge_order:
                state["discharge_order"] = discharge_order

        # 如果有查询，加入消息
        if request.query:
            from langchain_core.messages import HumanMessage
            state["messages"] = [HumanMessage(content=request.query)]

        # 带 retry 调用
        result = await self._invoke_with_retry(graph, state, config, agent_type)
        return result

    # 按 Agent 类型设定单次调用超时：重链路给更长窗口，避免误杀
    _AGENT_TIMEOUT = {
        "assessment": 300.0,   # 5 维度并行 + diagnose + summary，重链路
        "work_flow":  180.0,   # LLM 调用 + 可能多轮对话
        "alert":      120.0,
        "sop_qa":     120.0,
    }

    @classmethod
    async def _invoke_with_retry(cls, graph, state, config, agent_type):
        """带三层兜底的图调用。"""
        timeout = cls._AGENT_TIMEOUT.get(agent_type, 120.0)
        @with_retry(agent_type=agent_type, timeout_per_attempt=timeout)
        async def _invoke():
            return await graph.ainvoke(state, config=config)
        return await _invoke()

    # ── Pipeline 模式 ────────────────────────────────────────

    @staticmethod
    def _build_service_plan_from_assessment(
        assessment_result: Optional[dict], site_id: str,
        service_items: Optional[list[dict]] = None,
    ) -> Optional[dict]:
        """把 assessment 的 structured_output 转成 work_flow 期望的 service_plan 结构。

        work_flow 的 service_plan 字段（plan_id/site_id/risk_tier/daily_tasks/special_notes）
        必须保留，以兼容 prompt 模板；同时把评估的核心结果作为附加字段嵌入，
        让 work_flow 的 LLM 能基于评估结论给出针对性照护指导。

        **职责边界**（这条改动前后不一致，值得留意）：
          改造前 daily_tasks 由评估的 issues 派生（"[HIGH] 建议xxx"）——等于让 AI 决定
          "该做什么服务"，与"服务项目/频次属于机构业务配置"的边界冲突。
          现在 daily_tasks 只来自该护理等级绑定的**服务项**（service_items），
          评估结论退回到它该在的位置：护理等级 + 问题清单 + 注意事项。

        service_items 为空时 daily_tasks 为空（work_flow 会提示计划缺失），
        不再用评估问题拼一份看起来像计划的清单。

        若 assessment 触发 fallback 导致 structured_output 为 None，返回 None；
        上层 _run_pipeline 见 None 不写入 context，由 work_flow 如实报告计划缺失。
        """
        if not assessment_result:
            return None

        summary = assessment_result.get("summary") or {}
        issues = assessment_result.get("issues") or []
        structured = assessment_result.get("structured") or {}

        # 任务清单 = 业务配置的服务项（不是评估问题）
        from backend.core.service_plan_provider import LocalServicePlanProvider
        daily_tasks = LocalServicePlanProvider._tasks_from_items(service_items or [])

        return {
            # ── work_flow prompt 模板要求的字段 ──
            "plan_id": f"plan-{assessment_result.get('assessment_id', 'unknown')}",
            "site_id": site_id,
            "risk_tier": summary.get(
                "risk_tier_recommendation", structured.get("risk_tier", "二级"),
            ),
            "daily_tasks": daily_tasks,
            "special_notes": summary.get("overall_comment", ""),
            "service_items": service_items or [],
            # ── 附加：评估上下文，供 work_flow LLM 参考 ──
            "assessment_summary": summary,
            "weighted_score": assessment_result.get("weighted_score", 0),
            "dimension_scores": assessment_result.get("dimension_scores", []),
            "high_priority_issues": [
                i for i in issues if i.get("priority") == "high"
            ][:5],
        }

    async def _run_pipeline(self, request: AgentRequest) -> dict:
        """执行 Pipeline 模式。"""
        pipeline_name = request.pipeline or ""
        if pipeline_name not in PIPELINES:
            raise ValueError(f"未知 pipeline: {pipeline_name}")

        agents = PIPELINES[pipeline_name]
        results = {}
        context = dict(request.context)  # 复制上下文，用于 Agent 间传递

        for i, agent_type in enumerate(agents):
            logger.info(
                "pipeline.step",
                pipeline=pipeline_name, step=i + 1, total=len(agents),
                agent=agent_type.value,
            )

            # 构造子请求
            sub_request = request.model_copy()
            sub_request.agent_type = agent_type.value
            sub_request.mode = "single"
            sub_request.context = context

            result = await self._run_single_agent(sub_request, agent_type.value)
            results[agent_type.value] = result

            # 传递上下文（assessment → work_flow）
            if agent_type == AgentType.ASSESSMENT:
                assessment_result = result.get("structured_output")
                context["assessment_result"] = assessment_result

                # 评估结论 → 护理等级 → 该等级的服务项落到老人名下（业务配置驱动）
                # 这一步让 work_flow 的任务清单来自机构配置，而不是评估问题的复述
                risk_tier = (
                    ((assessment_result or {}).get("summary") or {})
                    .get("risk_tier_recommendation", "")
                )
                service_items: list[dict] = []
                if risk_tier:
                    from backend.core.service_plan_provider import get_service_plan_provider
                    try:
                        service_items = await get_service_plan_provider().ensure_items_for_level(
                            request.site_id, risk_tier,
                            request.tenant_id or "tenant_default",
                        )
                    except Exception as exc:  # noqa: BLE001
                        logger.warning("pipeline.bind_service_items_failed",
                                       site_id=request.site_id,
                                       risk_tier=risk_tier, error=str(exc))

                # 转成 work_flow 能直接用的 service_plan 结构
                service_plan = self._build_service_plan_from_assessment(
                    assessment_result, request.site_id, service_items=service_items,
                )
                if service_plan:
                    context["service_plan"] = service_plan
                    # 落库 service_plans：后续单 Agent 直调 work_flow 时可按
                    # site_id 读回最近一次计划；失败不阻断主链路（内存 plan 仍可用）
                    from backend.db.repositories import save_service_plan
                    persisted = await save_service_plan(
                        plan_id=service_plan.get("plan_id", ""),
                        site_id=request.site_id,
                        plan_content=service_plan,
                        tenant_id=request.tenant_id or "tenant_default",
                    )
                    logger.info(
                        "pipeline.context_transferred",
                        from_agent="assessment", to_agent="work_flow",
                        weighted_score=(assessment_result or {}).get("weighted_score"),
                        risk_tier=service_plan.get("risk_tier"),
                        daily_tasks_count=len(service_plan.get("daily_tasks", [])),
                        service_items=len(service_items),
                        service_plan_persisted=persisted,
                    )
                else:
                    # assessment 触发了 fallback（structured_output 为 None）：
                    # 不写 service_plan，work_flow 如实报告计划缺失
                    logger.warning(
                        "pipeline.context_transfer_skipped",
                        reason="assessment_fallback_no_structured_output",
                    )

        return await self._aggregate_pipeline(request, results)

    async def _aggregate_pipeline(self, request: AgentRequest, results: dict) -> dict:
        """汇总 Pipeline 结果。"""
        aggregated = {
            "mode": "pipeline",
            "pipeline": request.pipeline,
            "staff_id": request.staff_id,
            "site_id": request.site_id,
            "session_id": request.session_id,
            "results": results,
            "structured_output": {
                agent: result.get("structured_output", {})
                for agent, result in results.items()
            },
        }
        logger.info("pipeline.aggregated", agents=list(results.keys()))
        return aggregated

    # ── 意图识别（LLM 驱动 + 三层 fallback）─────────────────────

    async def _classify_intent(self, query: str) -> dict:
        """意图识别：调用 LLM 做结构化分类。

        返回 dict：
            {
                "agent_type": str,                  # 路由目标
                "confidence": float,                # 置信度 0-1
                "needs_clarification": bool,        # 是否需要澄清
                "candidates": list[str],           # 候选 Agent（澄清时用）
                "reasoning": str,                   # 分类依据
                "fallback_used": bool,             # 是否走了关键词降级
            }

        三层 fallback 由 IntentClassifier 内部处理：
            1. LLM 调用失败 → 关键词匹配降级
            2. 关键词也无强信号 → 默认 SOP_QA
            3. needs_clarification=True → 由 execute 上层处理
        """
        from backend.core.intent import classify_intent

        result = await classify_intent(query)
        return {
            "agent_type": result.agent_type.value,
            "confidence": result.confidence,
            "needs_clarification": result.needs_clarification,
            "candidates": [c.value for c in result.candidates],
            "reasoning": result.reasoning,
            "fallback_used": result.confidence <= 0.5,  # 降级标记
        }

    # ── 对外入口 ────────────────────────────────────────────

    async def execute(self, request: AgentRequest) -> dict:
        """统一执行入口。

        支持三种模式：
            - pipeline：走固定流水线（如 service_workflow = assessment → work_flow）
            - single + agent_type 指定：直接路由到指定 Agent
            - single + agent_type=None：LLM 意图识别后路由
                - needs_clarification=True 时返回 clarify 模式，让上层 UI 让用户选
        """
        mode = request.mode

        if mode == "pipeline":
            return await self._run_pipeline(request)

        # 单 Agent 模式
        agent_type = request.agent_type
        if not agent_type:
            # 意图识别（LLM 驱动 + 三层 fallback）
            intent = await self._classify_intent(request.query)
            logger.info(
                "orchestrator.intent_classified",
                query=request.query[:50],
                agent=intent["agent_type"],
                confidence=intent["confidence"],
                needs_clarification=intent["needs_clarification"],
                fallback_used=intent["fallback_used"],
            )

            # 澄清分支：跨域无法判断主意图，返回候选让用户选
            if intent["needs_clarification"]:
                candidates = intent["candidates"] or [intent["agent_type"]]
                return {
                    "mode": ExecutionMode.CLARIFY.value,
                    "query": request.query,
                    "candidates": candidates,
                    "message": (
                        f"您的问题可能涉及多个方向（{', '.join(candidates)}），"
                        "请明确您想做的事，或直接选择上方候选 Agent。"
                    ),
                    "reasoning": intent["reasoning"],
                    "session_id": request.session_id,
                    "staff_id": request.staff_id,
                }

            agent_type = intent["agent_type"]

        result = await self._run_single_agent(request, agent_type)
        # 注入路由信息，方便 API 层透传给前端
        if isinstance(result, dict):
            result.setdefault("agent_type", agent_type)
            result.setdefault("mode", "single")
        return result


# ──────────────────────────────────────────────────────────────
# 四、单例获取
# ──────────────────────────────────────────────────────────────

_orchestrator: Optional[Orchestrator] = None


def get_orchestrator() -> Orchestrator:
    """获取全局唯一的 Orchestrator 单例。"""
    global _orchestrator
    if _orchestrator is None:
        _orchestrator = Orchestrator()
    return _orchestrator
