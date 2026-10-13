# backend/core/memory.py
# 检查点（checkpointer）管理与对话摘要压缩工具
#
# 检查点存哪：默认 AsyncPostgresSaver（与业务库同一个 PostgreSQL，落 checkpoints /
# checkpoint_blobs / checkpoint_writes 三张表）；连不上或显式配置 memory 时回落进程内
# MemorySaver，保证单测、离线开发和"数据库还没起"的场景仍能跑。
#
# 跨 Agent 的隔离：图不同则 State schema 不同，同一个 thread_id 被两个 Agent 写入会互相
# 覆盖。内存实现靠"每个 Agent 一个实例"隔离，换到共享的 PostgreSQL 表之后这个隔离消失了，
# 因此改为在 config 里带 checkpoint_ns=<agent_type>——同一 thread_id 下按 Agent 分命名空间，
# thread_id 文案格式保持不变（见 build_config）。

import asyncio
from typing import Any, Optional

from langgraph.checkpoint.memory import MemorySaver
from langchain_core.messages import BaseMessage, HumanMessage, AIMessage, SystemMessage

from backend.config import get_settings
from backend.core.logger import get_logger

logger = get_logger(__name__)

# 进程内回落的 MemorySaver（按 Agent 隔离，与 PostgreSQL 后端的行为对齐）
_memory_savers: dict[str, MemorySaver] = {}

# PostgreSQL 检查点：全局一个连接池 + 一个 saver（表是共享的，隔离靠 thread_id + checkpoint_ns）
_pg_pool: Any = None
_pg_saver: Any = None
_saver_lock = asyncio.Lock()
_active_backend: str = "none"


def active_backend() -> str:
    """当前实际生效的检查点后端：postgres / memory / none（未初始化）。"""
    return _active_backend


async def _get_postgres_saver():
    """惰性创建 AsyncPostgresSaver（含连接池与建表），失败抛异常由调用方决定是否回落。"""
    global _pg_pool, _pg_saver, _active_backend
    if _pg_saver is not None:
        return _pg_saver

    async with _saver_lock:
        if _pg_saver is not None:          # 双检：并发首调只建一次
            return _pg_saver

        from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
        from psycopg.rows import dict_row
        from psycopg_pool import AsyncConnectionPool

        settings = get_settings()
        pool = AsyncConnectionPool(
            conninfo=settings.checkpoint_dsn,
            min_size=settings.checkpoint_pool_min_size,
            max_size=settings.checkpoint_pool_max_size,
            # autocommit + dict_row 是 AsyncPostgresSaver 的官方要求：
            # saver 自己管事务边界，行要按 dict 取字段
            kwargs={"autocommit": True, "row_factory": dict_row},
            open=False,
        )
        await pool.open(wait=True, timeout=10)
        saver = AsyncPostgresSaver(pool)
        await saver.setup()                # 幂等建表：checkpoint_migrations 记录版本

        _pg_pool, _pg_saver = pool, saver
        _active_backend = "postgres"
        logger.info("memory.postgres_saver_ready",
                    host=settings.db_host, port=settings.db_port, db=settings.db_name)
        return _pg_saver


async def get_memory_saver(agent_type: str = "default"):
    """
    获取检查点存储（进程内单例，异步初始化）。

    后端选择由 settings.checkpoint_backend 决定：
      auto     → 先试 PostgreSQL，失败回落内存并打警告
      postgres → 只用 PostgreSQL，失败直接抛（生产环境，避免静默丢状态）
      memory   → 只用内存（单测 / 无库环境）

    Args:
        agent_type: Agent 标识符，如 "sop_qa" / "alert" / "work_flow"；
                    内存后端用它隔离实例，PostgreSQL 后端仅用于日志

    Returns:
        传给 StateGraph.compile(checkpointer=...) 的 saver
    """
    global _active_backend
    backend = (get_settings().checkpoint_backend or "auto").strip().lower()

    if backend != "memory":
        try:
            return await _get_postgres_saver()
        except Exception as e:
            if backend == "postgres":
                raise
            logger.warning("memory.postgres_unavailable", agent=agent_type,
                           error=str(e)[:160], fallback="memory")

    if agent_type not in _memory_savers:
        _memory_savers[agent_type] = MemorySaver()
        _active_backend = "memory"
        logger.info("memory.saver_initialized", agent=agent_type, backend="memory")
    return _memory_savers[agent_type]


async def delete_thread_state(thread_id: str) -> dict:
    """
    原子删除整个线程的检查点：该 thread_id 下的所有 checkpoints、blobs、writes 一次清掉。

    适用场景：确认这个照护周期已彻底结束，且审计期已过，不再需要任何会话状态
    （包括暂停中的 interrupt 断点）。不适合"只想重开一轮对话"——那用新的 session_id 即可，
    不要删历史。

    注意：删除按 thread_id 整体生效，会连带该线程下所有 Agent 命名空间的检查点，
    因此调用方必须先做业务侧确认（见 DELETE /api/v1/chat/session/{site_id}/{session_id}）。

    Returns:
        {"thread_id": ..., "deleted": bool, "backend": "postgres"|"memory"}
    """
    if _pg_saver is not None:
        await _pg_saver.adelete_thread(thread_id)
        logger.info("memory.thread_deleted", thread_id=thread_id, backend="postgres")
        return {"thread_id": thread_id, "deleted": True, "backend": "postgres"}

    # 内存后端：MemorySaver 也实现了 adelete_thread，逐个 Agent 实例清（与后端无关的调用方式）
    deleted = False
    for saver in _memory_savers.values():
        await saver.adelete_thread(thread_id)
        deleted = True
    logger.info("memory.thread_deleted", thread_id=thread_id, backend="memory",
                agents=len(_memory_savers))
    return {"thread_id": thread_id, "deleted": deleted, "backend": "memory"}


async def close_memory_savers() -> None:
    """关闭 PostgreSQL 连接池（应用 shutdown 时调用；内存后端无需处理）。"""
    global _pg_pool, _pg_saver, _active_backend
    if _pg_pool is not None:
        await _pg_pool.close()
        logger.info("memory.postgres_pool_closed")
    _pg_pool, _pg_saver = None, None
    if _active_backend == "postgres":
        _active_backend = "none"


def build_thread_id(site_id: str, session_id: str) -> str:
    """
    构建 LangGraph Checkpointer 使用的 thread_id。

    格式：site_{site_id}_session_{session_id}
    同一老人的不同会话有独立的历史，互不干扰。

    Example:
        build_thread_id("e001", "sess-abc")
        → "site_e001_session_sess-abc"
    """
    return f"site_{site_id}_session_{session_id}"


def build_config(site_id: str, session_id: str, agent_type: Optional[str] = None) -> dict:
    """
    构建 LangGraph 调用所需的 config 字典。

    用法：
        config = build_config(site_id, session_id, agent_type="alert")
        result = await graph.ainvoke(state, config=config)

    Args:
        site_id:   老人 ID
        session_id: 会话 ID（告警场景传 alert_id）
        agent_type: Agent 标识。传入后写入 checkpoint_ns，
                    让同一 thread_id 下不同 Agent 的检查点互不覆盖（见文件头说明）。
                    触发与恢复（interrupt / Command(resume)）必须用同一个 agent_type，
                    否则会读不到断点——和 thread_id 不一致是同一类错。

    Returns:
        {"configurable": {"thread_id": "site_xxx_session_yyy", "checkpoint_ns": "alert"}}
    """
    configurable: dict[str, str] = {
        "thread_id": build_thread_id(site_id, session_id),
    }
    if agent_type:
        configurable["checkpoint_ns"] = agent_type
    return {"configurable": configurable}


def trim_messages_to_window(
    messages: list[BaseMessage],
    window_size: int = 10,
) -> list[BaseMessage]:
    """
    滑动窗口裁剪：保留最近 window_size 轮对话。
    SystemMessage 始终保留在最前，不受 window_size 限制。

    Args:
        messages:    当前完整消息列表
        window_size: 保留的对话轮数（1轮 = 1 Human + 1 AI），默认 10 轮

    Returns:
        裁剪后的消息列表
    """
    system_messages = [m for m in messages if isinstance(m, SystemMessage)]
    dialogue_messages = [m for m in messages if not isinstance(m, SystemMessage)]
    max_dialogue_messages = window_size * 2   # 1轮 = 2条消息

    if len(dialogue_messages) <= max_dialogue_messages:
        return messages   # 未超出窗口，不裁剪

    trimmed = dialogue_messages[-max_dialogue_messages:]

    logger.info(
        "memory.window_trimmed",
        original=len(dialogue_messages),
        kept=len(trimmed),
        window_size=window_size,
    )

    return system_messages + trimmed


def should_trigger_summary(
    messages: list[BaseMessage],
    covered_count: int = 0,
    threshold: int = 10,
) -> bool:
    """
    判断是否该（重新）做摘要压缩。

    Args:
        messages:      当前消息列表
        covered_count: 已被现有摘要覆盖的消息条数（state.summary_message_count）
        threshold:     触发压缩的轮数阈值，默认 10 轮

    Returns:
        True → 需要压缩

    判据是"距上次摘要新增了多少轮"，不是"总条数是不是某个整数的倍数"：
    旧实现是 `dialogue_count % (threshold * 2) == 0`，只有恰好落在 20、40… 那条消息上才触发，
    错过一次就再也不补，而且 0 条消息也会返回 True。现在改成"新增对话 ≥ threshold 轮就压一次"，
    压完由 save_memory 记下覆盖条数，长会话稳定每 10 轮压一次，短会话不触发。
    """
    dialogue_count = sum(1 for m in messages if isinstance(m, (HumanMessage, AIMessage)))
    return (dialogue_count - (covered_count or 0)) >= threshold * 2   # 每轮 2 条


async def compress_to_summary(
    messages: list[BaseMessage],
    existing_summary: Optional[str] = None,
) -> str:
    """
    将历史对话压缩为结构化老人照护画像摘要。

    增量压缩：传入 existing_summary 防止已记录内容被重复写入。

    Args:
        messages:         待压缩的历史消息列表
        existing_summary: 上次的摘要（可选）

    Returns:
        压缩后的摘要文本
    """
    from langchain_core.messages import HumanMessage as LCHuman
    from backend.core.llm_factory import get_llm

    SUMMARY_PROMPT = """请将以下护理员与 AI 的对话压缩为结构化老人照护画像摘要。

【压缩规则】
必须保留：老人明确的健康异常 / 反复出现的护理需求 / 照护偏好新增信息
选择性保留：已稳定的照护项目（简短标注）/ 照护进度信息
可以丢弃：已在上次摘要记录的内容 / 闲聊 / 已解决且无变化的问题

【上一次摘要】
{previous_summary}

【本次新增对话】
{new_conversations}

请直接输出摘要文本，不要加任何前缀。"""

    conversation_text = "\n".join([
        f"{'护理员' if isinstance(m, HumanMessage) else 'AI'}：{m.text if hasattr(m, 'text') and not callable(m.text) else str(m.content)}"
        for m in messages
        if isinstance(m, (HumanMessage, AIMessage))
    ])

    prompt_text = SUMMARY_PROMPT.format(
        previous_summary=existing_summary or "（无上次摘要）",
        new_conversations=conversation_text,
    )

    llm = get_llm("summarize")
    response = await llm.ainvoke([LCHuman(content=prompt_text)])
    summary = (
        response.content if isinstance(response.content, str)
        else str(response.content)
    ).strip()

    logger.info(
        "memory.summary_generated",
        input_messages=len(messages),
        summary_length=len(summary),
    )
    return summary
