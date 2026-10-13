# backend/api/v1/unified_chat.py
# 统一对话入口路由

import json
import uuid

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sse_starlette.sse import EventSourceResponse

from backend.dependencies import get_current_user
from backend.core.orchestrator import get_orchestrator, AgentRequest
from backend.core.logger import get_logger
from backend.core.memory import build_thread_id, delete_thread_state

logger = get_logger(__name__)

router = APIRouter(prefix="/chat")


class UnifiedChatRequest(BaseModel):
    """统一对话请求。"""
    elder_id:   str = ""
    session_id: str = ""
    query:      str
    agent_type: str | None = None   # 指定 Agent（None = 自动路由）
    mode:       str = "single"
    pipeline:   str | None = None


@router.post("")
async def unified_chat(
    req: UnifiedChatRequest,
    current_user: dict = Depends(get_current_user),
):
    """统一对话入口（SSE 流式响应）。由 Orchestrator 自动路由到对应 Agent。"""
    session_id = req.session_id or f"chat-{uuid.uuid4().hex[:12]}"

    async def event_generator():
        yield {"event": "session", "data": json.dumps({"session_id": session_id}, ensure_ascii=False)}

        try:
            orchestrator = get_orchestrator()
            request = AgentRequest(
                staff_id=current_user["user_id"],
                elder_id=req.elder_id,
                tenant_id=current_user["tenant_id"],
                session_id=session_id,
                agent_type=req.agent_type,
                mode=req.mode,
                pipeline=req.pipeline,
                query=req.query,
            )
            result = await orchestrator.execute(request)

            # ── 分支 1：clarify 模式（意图识别跨域无法判断）──
            if result.get("mode") == "clarify":
                yield {
                    "event": "clarify",
                    "data": json.dumps({
                        "candidates": result.get("candidates", []),
                        "message":    result.get("message", ""),
                        "reasoning":  result.get("reasoning", ""),
                        "query":      result.get("query", req.query),
                    }, ensure_ascii=False),
                }
                yield {"event": "done", "data": ""}
                return

            # ── 分支 2：正常答案 ──
            structured = result.get("structured_output", {})
            answer = (
                result.get("answer", "")
                or structured.get("answer", "")
                or result.get("content", "")
                or "处理完成"
            )

            yield {
                "event": "answer",
                "data": json.dumps({
                    "content": answer,
                    "agent_type": result.get("agent_type") or req.agent_type or "auto",
                    "mode":       result.get("mode", req.mode),
                    "structured_output": structured,
                    "fallback_used": result.get("fallback_used", False),
                }, ensure_ascii=False),
            }
        except Exception as e:
            yield {"event": "error", "data": json.dumps({"error": str(e)}, ensure_ascii=False)}

        yield {"event": "done", "data": ""}

    return EventSourceResponse(event_generator())


@router.delete("/session/{elder_id}/{session_id}")
async def delete_chat_session(
    elder_id: str,
    session_id: str,
    current_user: dict = Depends(get_current_user),
):
    """删除整个会话线程的检查点（含暂停中的断点与所有 Agent 命名空间）。

    什么时候用：确认该照护周期已彻底结束，且审计期已过，不再需要任何会话状态。

    什么时候别用：只是"想重新开始一轮对话"——那就换个 session_id，不要删历史；
    一旦删除不可恢复，暂停中的告警断点也会一起消失。

    权限：仅 admin（破坏性操作）；租户：老人必须归属当前租户，跨租户视为不存在。
    """
    if current_user["role"] != "admin":
        raise HTTPException(status_code=403, detail="仅管理员可删除会话状态")

    from backend.db.repositories import get_elder_tenant
    owner_tenant = await get_elder_tenant(elder_id)
    if owner_tenant is None or owner_tenant != current_user["tenant_id"]:
        raise HTTPException(status_code=404, detail=f"老人不存在: {elder_id}")

    thread_id = build_thread_id(elder_id, session_id)
    result = await delete_thread_state(thread_id)
    logger.info("chat.session_deleted", elder_id=elder_id, session_id=session_id,
                thread_id=thread_id, operator=current_user["user_id"],
                backend=result.get("backend"))

    return {
        "thread_id": thread_id,
        "deleted":  result.get("deleted", False),
        "backend":  result.get("backend"),
        "message":  "已删除该会话线程的全部检查点" if result.get("deleted")
                    else "该会话线程没有检查点（可能本就没跑过，或已被删除）",
    }
