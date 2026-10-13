# backend/api/v1/sop_qa.py
# 护理 SOP 问答 Agent API 路由（SSE 流式）

import json
import uuid

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sse_starlette.sse import EventSourceResponse

from backend.dependencies import get_current_user
from backend.core.orchestrator import get_orchestrator, AgentRequest

router = APIRouter(prefix="/sop-qa")


class SOPQARequest(BaseModel):
    """SOP 问答请求。"""
    site_id:   str = ""
    session_id: str = ""
    query:      str
    org_id:     str | None = None


@router.post("/chat")
async def sop_qa_chat(
    req: SOPQARequest,
    current_user: dict = Depends(get_current_user),
):
    """护理 SOP 问答（SSE 流式响应）。"""
    session_id = req.session_id or f"sopqa-{uuid.uuid4().hex[:12]}"

    async def event_generator():
        yield {"event": "session", "data": json.dumps({"session_id": session_id}, ensure_ascii=False)}

        try:
            orchestrator = get_orchestrator()
            request = AgentRequest(
                staff_id=current_user["user_id"],
                site_id=req.site_id,
                tenant_id=current_user["tenant_id"],
                session_id=session_id,
                agent_type="sop_qa",
                mode="single",
                query=req.query,
                context={"org_id": req.org_id},
            )
            result = await orchestrator.execute(request)

            answer = result.get("answer", result.get("structured_output", {}).get("answer", ""))
            answer_mode = result.get("answer_mode", result.get("structured_output", {}).get("answer_mode", ""))
            sources = result.get("sources", result.get("structured_output", {}).get("sources", []))

            yield {
                "event": "answer",
                "data": json.dumps({
                    "content": answer,
                    "answer_mode": answer_mode,
                    "sources": sources,
                }, ensure_ascii=False),
            }
        except Exception as e:
            yield {
                "event": "error",
                "data": json.dumps({"error": str(e)}, ensure_ascii=False),
            }

        yield {"event": "done", "data": ""}

    return EventSourceResponse(event_generator())
