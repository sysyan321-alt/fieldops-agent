# backend/core/retry.py
# 三层兜底机制：自动重试 → Agent 级降级 → 系统级兜底

import asyncio                                   # 异步：用于超时控制和等待
from functools import wraps                      # @wraps：装饰器里保留原函数的名字/文档
from typing import Callable, Any, Optional       # 类型注解：可调用对象 / 任意 / 可选

from backend.core.exceptions import (            # 引入统一异常体系
    LLMAPIError,
    MilvusConnectionError,
    InvalidInputError,
    AuthenticationError,
)
from backend.core.logger import get_logger, configure_logging
logger = get_logger(__name__)

# ── 异常分类 ───────────────────────────────────────────────
# 可重试：多半是短暂故障（网络抖动、超时），重试一下可能就好
RETRYABLE_ERRORS = (
    LLMAPIError,
    MilvusConnectionError,
    TimeoutError,
    ConnectionError,
)
# 不可重试：重试也没用（输入非法、认证失败），应立即抛出
NON_RETRYABLE_ERRORS = (
    InvalidInputError,
    AuthenticationError,
)

MAX_RETRIES = 2                  # 最多重试 2 次（加上首次 = 共 3 次尝试）
RETRY_DELAYS = [1.0, 3.0]        # 第 1 次重试前等 1 秒，第 2 次前等 3 秒
TIMEOUT_PER_ATTEMPT = 120.0       # 单次调用最多等 120 秒，超时算失败



def with_retry(agent_type: str = "", timeout_per_attempt: float = TIMEOUT_PER_ATTEMPT):
    """三层兜底装饰器工厂。给异步函数套上「重试 → 降级 → 系统兜底」三层保护。

    参数：
        agent_type:           Agent 类型（用于选降级策略和日志）
        timeout_per_attempt:  单次调用超时秒数；重链路 Agent（如 assessment
                              5 维度并行 + diagnose + summary）可传更大值。

    用法：
        @with_retry(agent_type="assessment", timeout_per_attempt=300.0)
        async def _invoke():
            return await graph.ainvoke(state, config=config)
    """
    def decorator(func: Callable) -> Callable:       # 中间层：接收被装饰的函数
        @wraps(func)                                 # 保留原函数的元信息（名字、docstring）
        async def wrapper(*args, **kwargs) -> Any:   # 最内层：真正的执行逻辑

            # ── 第一层：自动重试 ──────────────────────────
            last_error: Optional[Exception] = None   # 记录最后一次的错误，留给后面降级用
            for attempt in range(MAX_RETRIES + 1):   # 循环 3 次：attempt = 0, 1, 2
                try:
                    result = await asyncio.wait_for(
                        func(*args, **kwargs),
                        timeout=timeout_per_attempt,
                    )
                    if attempt > 0:                  # 如果是重试后成功的，记一条日志
                        logger.info("retry.succeeded", agent_type=agent_type, attempt=attempt + 1)
                    return result                    # 成功，直接返回，结束

                except NON_RETRYABLE_ERRORS as e:    # 不可重试异常：立即抛出，不再重试
                    logger.warning("retry.non_retryable_error", agent_type=agent_type, error=str(e))
                    raise

                except Exception as e:               # 其它（可重试）异常
                    last_error = e
                    if attempt < MAX_RETRIES:        # 还没到上限：等待后重试
                        delay = RETRY_DELAYS[attempt]
                        logger.warning(
                            "retry.attempt_failed", agent_type=agent_type,
                            attempt=attempt + 1, max_retries=MAX_RETRIES, delay=delay, error=str(e),
                        )
                        await asyncio.sleep(delay)
                    else:                            # 到上限了：记录失败，跳出循环去降级
                        logger.error("retry.all_attempts_failed", agent_type=agent_type, error=str(e))

            # ── 第二层：Agent 级降级 ──────────────────────
            try:
                fallback_result = await AgentFallbackHandler.handle(
                    agent_type=agent_type, original_error=last_error,
                )
                logger.info("retry.fallback_succeeded", agent_type=agent_type)
                return fallback_result
            except Exception as fallback_error:
                logger.error("retry.fallback_failed", agent_type=agent_type, error=str(fallback_error))

            # ── 第三层：系统级兜底 ────────────────────────
            logger.error("retry.system_fallback", agent_type=agent_type, original_error=str(last_error))
            return _system_fallback_response(agent_type)
        return wrapper
    return decorator



class AgentFallbackHandler:
    """第二层降级：各 Agent 的专项降级策略（尽量保留核心功能，退化为更简单的实现）。"""

    @classmethod
    async def handle(cls, agent_type: str, original_error: Exception) -> Any:
        """根据 agent_type 选择对应的降级策略。"""
        fallback_map = {                              # 类型 → 降级方法 的映射表
            "assessment":  cls._assessment_fallback,
            "sop_qa":       cls._sop_qa_fallback,
            "alert":        cls._alert_fallback,
            "care_flow":    cls._care_flow_fallback,
        }
        handler = fallback_map.get(agent_type)        # 查表
        if handler:
            return await handler()
        raise original_error                          # 没有对应降级策略，原样抛出（交给系统兜底）

    @classmethod
    async def _assessment_fallback(cls) -> dict:
        """评估降级：评估服务不可用，提示联系护理主管手动评估。"""
        logger.info("fallback.assessment_service_unavailable")
        return {
            "fallback_used": True,
            "content": "⚠️ 评估服务暂时不可用，请稍后重试或联系护理主管手动评估。",
            "structured_output": None,
        }

    @classmethod
    async def _sop_qa_fallback(cls) -> dict:
        """护理问答降级：知识库检索不可用，提示查阅纸质护理手册。"""
        logger.info("fallback.sop_qa_service_unavailable")
        return {
            "fallback_used": True,
            "content": "⚠️ 护理知识库检索暂时不可用，请稍后重试或查阅纸质护理手册。",
            "structured_output": None,
        }

    @classmethod
    async def _alert_fallback(cls) -> dict:
        """告警研判降级：标记为需站长人工核实。"""
        logger.info("fallback.alert_basic")
        return {
            "fallback_used": True,
            "content": "⚠️ 告警研判服务暂时不可用，已标记为需站长人工核实。",
            "structured_output": None,
        }

    @classmethod
    async def _care_flow_fallback(cls) -> dict:
        """照护流程降级：记录本次对话，提示稍后查看。"""
        logger.info("fallback.care_flow_basic_feedback")
        return {
            "fallback_used": True,
            "content": "⚠️ 照护流程服务暂时不可用，已记录本次对话，请稍后查看。",
            "structured_output": None,
        }


def _system_fallback_response(agent_type: str) -> dict:
    """第三层：系统级兜底。所有降级都失败后返回它，保证用户始终能收到响应。"""
    messages = {                                      # 按 agent_type 给不同的友好提示
        "assessment":  "非常抱歉，评估服务暂时不可用，请稍后再试，或联系护理主管手动评估。",
        "sop_qa":      "非常抱歉，护理问答服务暂时不可用，请稍后再试，或查阅纸质护理手册。",
        "alert":       "非常抱歉，告警研判服务暂时不可用，本次告警已转交站长人工核实。",
        "care_flow":   "非常抱歉，照护流程服务暂时不可用，请稍后重新开始。",
    }
    content = messages.get(agent_type, "服务暂时不可用，请稍后再试。")  # 找不到就用通用提示
    return {
        "messages": [],
        "content": content,
        "fallback_used": True,
        "system_fallback": True,                      # 标记：走到了最后一层系统兜底
        "structured_output": None,
    }
