# backend/core/intent.py
# 意图识别模块：把用户的一句话路由到合适的 Agent。
#
# 设计原则（轻量，不开 LangGraph）：
#   - 意图识别是单次 LLM 调用，不需要状态机
#   - 三层 fallback：① LLM 调用失败 → 关键词匹配降级
#                     ② 关键词也无强信号 → 默认 SOP_QA（通用问答兜底）
#                     ③ needs_clarification=True → 返回 CLARIFY 标记给上层处理
#   - 复用 llm_factory 的 "intent" 路由（with_structured_output + function_calling）
#
# 触发场景：
#   - orchestrator.execute(request) 入口
#   - 仅当 mode=single 且 request.agent_type=None 时调用（开放对话入口）

from __future__ import annotations
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field

from backend.core.logger import get_logger
from backend.core.llm_factory import get_structured_llm
from backend.config import get_settings
from backend.core.cache import get_cache, hash_text, make_key

logger = get_logger(__name__)


# ──────────────────────────────────────────────────────────────
# 一、结构化输出 Schema
# ──────────────────────────────────────────────────────────────

class IntentAgentType(str, Enum):
    """意图识别可路由到的 Agent（与 orchestrator.AgentType 对齐）。"""
    ASSESSMENT = "assessment"   # 点位风险评级
    SOP_QA     = "sop_qa"       # 作业规范问答 / 通用问答兜底
    ALERT      = "alert"        # 告警研判
    WORK_FLOW  = "work_flow"   # 作业流程


class IntentClassification(BaseModel):
    """意图识别的结构化输出。LLM 必须填充以下字段。"""

    agent_type: IntentAgentType = Field(
        ...,
        description=(
            "路由目标 Agent。"
            "assessment=点位风险评级（明确要做评估/复评动作）；"
            "alert=设备告警/心率异常/跌倒事件实时研判；"
            "work_flow=服务计划/建档退场流程执行；"
            "sop_qa=作业规范知识问答或通用闲聊兜底。"
        ),
    )
    confidence: float = Field(
        ...,
        ge=0.0, le=1.0,
        description="置信度 0-1，>=0.6 视为可路由，<0.6 应触发澄清。",
    )
    needs_clarification: bool = Field(
        default=False,
        description=(
            "是否需要向用户澄清。"
            "仅当查询跨多个 Agent 且无法判断主意图时设 True；"
            "闲聊、明确单域问题一律 False。"
        ),
    )
    candidates: list[IntentAgentType] = Field(
        default_factory=list,
        description=(
            "needs_clarification=True 时给出 2-3 个候选 Agent 供用户选择；"
            "False 时留空列表。"
        ),
    )
    reasoning: str = Field(
        default="",
        description="一句话解释分类依据（便于日志排查）。",
    )


# ──────────────────────────────────────────────────────────────
# 二、Prompt 模板（few-shot）
# ──────────────────────────────────────────────────────────────

INTENT_SYSTEM_PROMPT = """你是 FieldOps 现场作业智能体的意图识别模块。
你的任务：把作业人员/值班主管的一句话路由到最合适的下游 Agent。

【下游 Agent 职责】
- assessment：点位风险评级。触发词：要做评估、复评、结构/设备/环境/作业风险「评估动作」。
- alert：设备告警实时研判。触发词：过载、设备离线、跌倒事件、人员闯入、可燃气报警、紧急情况。
- work_flow：作业流程执行。触发词：服务计划、建档流程、退场流程、日常巡检任务、阶段执行。
- sop_qa：作业规范知识问答 + 通用闲聊兜底。问操作规程、处置步骤、规范条款、或非业务闲聊。

【跨域冲突样本（重点）】
1. "评一下这个点位的作业风险" → ASSESSMENT（明确要做评估动作）
2. "高处作业要系安全带吗" → SOP_QA（问规范条款，不是评估动作）
3. "有人在屋面坠落了" → ALERT（实时事件告警）
4. "触电后怎么急救" → SOP_QA（问处置规范）
5. "B1 配电间过载告警" → ALERT（实时事件告警）
6. "配电柜多久巡检一次" → SOP_QA（问规范条款）
7. "明天执行建档流程" → WORK_FLOW（流程执行）
8. "你是谁" → SOP_QA（闲聊兜底）
9. "今天天气" → SOP_QA（非业务兜底）
10. "评估一下这个点位的服务计划" → needs_clarification=True,
    candidates=[ASSESSMENT, WORK_FLOW]（跨域，无法判断主意图）

【判定规则】
1. 明确单域问题 → confidence>=0.8, needs_clarification=False, candidates=[]
2. 跨域但能判断主意图 → confidence 0.6-0.8, needs_clarification=False, candidates=[]
3. 跨域无法判断主意图 → needs_clarification=True, candidates 必须 ≥2 个
   - 硬约束：candidates 长度必须 >= 2，且必须包含 agent_type 之外的其他 Agent
   - 例：agent_type=ASSESSMENT 时 candidates 至少含 [ASSESSMENT, WORK_FLOW/SOP_QA/ALERT] 等两个以上
   - 只有 1 个候选时不应触发 clarify，应直接路由到该 Agent
4. 闲聊/非业务 → SOP_QA, confidence>=0.7, needs_clarification=False, candidates=[]

输出必须严格符合 IntentClassification schema。"""


def _build_user_prompt(query: str, history: Optional[list[dict]] = None) -> str:
    """构造 user prompt，附加最近 1-2 轮历史对话作上下文。"""
    parts = [f"用户输入：{query}"]
    if history:
        # 只取最近 2 轮（user/assistant），避免 prompt 过长
        recent = history[-2:]
        history_text = "\n".join(
            f"  {m.get('role', 'user')}: {m.get('content', '')[:80]}"
            for m in recent
        )
        parts.append(f"最近对话（供参考）：\n{history_text}")
    parts.append("请输出 IntentClassification。")
    return "\n\n".join(parts)


# ──────────────────────────────────────────────────────────────
# 三、关键词匹配降级（Layer 1 fallback）
# ──────────────────────────────────────────────────────────────

# 关键词优先级：评估 > 告警 > 照护流程 > SOP_QA 兜底
# 注意：跨域冲突样本（如"评估跌倒风险"）关键词无法处理，会落到 SOP_QA 兜底
_KEYWORD_MAP: list[tuple[list[str], IntentAgentType]] = [
    (["评估", "评分", "能力评估", "建档评估", "复评", "ADL"], IntentAgentType.ASSESSMENT),
    (["告警", "报警", "心率异常", "跌倒告警", "离床", "设备报警"], IntentAgentType.ALERT),
    (["作业流程", "服务计划", "日常巡检", "建档流程", "退场"], IntentAgentType.WORK_FLOW),
]


def _keyword_fallback(query: str) -> IntentClassification:
    """关键词匹配降级（保留旧逻辑作为 LLM 失败时的兜底）。"""
    for keywords, agent in _KEYWORD_MAP:
        if any(k in query for k in keywords):
            return IntentClassification(
                agent_type=agent,
                confidence=0.5,   # 降级时置信度降级标记
                needs_clarification=False,
                reasoning=f"keyword_fallback 命中: {agent.value}",
            )
    # 默认兜底：SOP_QA
    return IntentClassification(
        agent_type=IntentAgentType.SOP_QA,
        confidence=0.3,
        needs_clarification=False,
        reasoning="keyword_fallback 默认兜底: sop_qa",
    )


# ──────────────────────────────────────────────────────────────
# 四、IntentClassifier 主类
# ──────────────────────────────────────────────────────────────

def _normalize_clarification(result: IntentClassification) -> IntentClassification:
    """后处理：修复 LLM 返回的不合规 clarify 结果。

    硬约束：needs_clarification=True 时 candidates 必须 ≥2 个，
            且必须包含 agent_type 之外的其他 Agent（否则 clarify 无意义）。

    修复策略（顺序很重要）：
        1. 去重候选，并确保 agent_type 本身在 candidates 里
        2. 尝试补 SOP_QA 作为通用兜底候选（SOP_QA != agent_type 时）
        3. 若补完后仍只有 1 个候选（即 agent_type=SOP_QA 且无其他）→ 降级为非 clarify
        4. 截断到最多 3 个候选
    """
    if not result.needs_clarification:
        return result

    # 去重候选
    seen = set()
    unique_candidates: list[IntentAgentType] = []
    for c in result.candidates:
        if c not in seen:
            seen.add(c)
            unique_candidates.append(c)

    # 必须含 agent_type 本身（用户至少要能看到主候选）
    if result.agent_type not in seen:
        unique_candidates.insert(0, result.agent_type)
        seen.add(result.agent_type)

    # 候选不足 2 个 → 先尝试补 SOP_QA 作为通用兜底候选
    if len(unique_candidates) < 2 and IntentAgentType.SOP_QA not in seen:
        unique_candidates.append(IntentAgentType.SOP_QA)
        seen.add(IntentAgentType.SOP_QA)

    # 检查是否至少有 1 个非 agent_type 的其他候选
    others = [c for c in unique_candidates if c != result.agent_type]
    if not others:
        # 补完 SOP_QA 后仍无其他候选（仅当 agent_type=SOP_QA 且原 candidates 为空或只含 SOP_QA）
        # → clarify 无意义，降级为直接路由
        logger.warning(
            "intent.clarify_downgraded_no_other_candidate",
            agent=result.agent_type.value,
            original_candidates=[c.value for c in result.candidates],
        )
        return result.model_copy(update={
            "needs_clarification": False,
            "candidates": [],
            "reasoning": f"{result.reasoning} [postprocess: clarify 降级，无其他候选]",
        })

    # 候选 >=2，截断到最多 3 个（避免给用户太多选择）
    final_candidates = unique_candidates[:3]

    if len(final_candidates) != len(result.candidates) or final_candidates != result.candidates:
        logger.info(
            "intent.clarify_candidates_normalized",
            original=[c.value for c in result.candidates],
            normalized=[c.value for c in final_candidates],
        )
        return result.model_copy(update={"candidates": final_candidates})

    return result


class IntentClassifier:
    """意图识别器：调用 LLM 做结构化分类，三层 fallback。"""

    # 单次 LLM 调用超时（意图识别应当快，超过 30s 就降级）
    _LLM_TIMEOUT_SECONDS = 30.0

    @classmethod
    async def classify(
        cls,
        query: str,
        history: Optional[list[dict]] = None,
    ) -> IntentClassification:
        """对用户输入做意图分类。

        三层 fallback：
            1. LLM 调用失败（超时/异常）→ 关键词匹配降级
            2. 关键词也无强信号 → 默认 SOP_QA（已在 _keyword_fallback 内处理）
            3. needs_clarification=True → 由上层 orchestrator 处理
        """
        if not query or not query.strip():
            # 空查询直接兜底
            return IntentClassification(
                agent_type=IntentAgentType.SOP_QA,
                confidence=0.1,
                needs_clarification=False,
                reasoning="empty_query",
            )

        # ── Layer -1：Redis 缓存（相同问题不重复烧 LLM；带 history 的调用不缓存）──
        cache = get_cache()
        cache_key = None
        if not history:
            cache_key = make_key("intent", hash_text(query))
            cached = await cache.get_json(cache_key)
            if cached:
                try:
                    hit = IntentClassification(**cached)
                    logger.info("intent.cache_hit", query=query[:50],
                                agent=hit.agent_type.value, confidence=hit.confidence)
                    return hit
                except Exception as e:
                    logger.warning("intent.cache_decode_failed", error=str(e)[:120])

        # ── Layer 0：LLM 结构化分类 ──
        try:
            import asyncio
            structured_llm = get_structured_llm("intent", IntentClassification)
            user_prompt = _build_user_prompt(query, history)
            messages = [
                ("system", INTENT_SYSTEM_PROMPT),
                ("user", user_prompt),
            ]
            result = await asyncio.wait_for(
                structured_llm.ainvoke(messages),
                timeout=cls._LLM_TIMEOUT_SECONDS,
            )
            # 校验：LLM 返回的必须是 IntentClassification 实例
            if not isinstance(result, IntentClassification):
                raise TypeError(f"LLM 返回类型错误: {type(result).__name__}")

            # 后处理：修复 LLM 不合规的 clarify 结果（candidates < 2 等）
            result = _normalize_clarification(result)

            logger.info(
                "intent.classified",
                query=query[:50],
                agent=result.agent_type.value,
                confidence=result.confidence,
                needs_clarification=result.needs_clarification,
                candidates=[c.value for c in result.candidates],
                reasoning=result.reasoning[:80],
            )
            if cache_key:
                await cache.set_json(
                    cache_key, result.model_dump(mode="json"),
                    ttl_seconds=get_settings().cache_ttl_intent,
                )
            return result

        except Exception as e:
            # ── Layer 1：LLM 失败 → 关键词匹配降级 ──
            logger.warning(
                "intent.llm_failed_fallback_to_keyword",
                query=query[:50], error=str(e)[:120],
            )
            return _keyword_fallback(query)


# ──────────────────────────────────────────────────────────────
# 五、便捷入口
# ──────────────────────────────────────────────────────────────

async def classify_intent(
    query: str, history: Optional[list[dict]] = None,
) -> IntentClassification:
    """意图识别便捷入口。"""
    return await IntentClassifier.classify(query, history=history)
