# backend/api/router.py
# 聚合所有 v1 路由

from fastapi import APIRouter

from backend.api.v1.auth import router as auth_router
from backend.api.v1.assessment import router as assessment_router
from backend.api.v1.sop_qa import router as sop_qa_router
from backend.api.v1.alert import router as alert_router
from backend.api.v1.devices import router as devices_router
from backend.api.v1.work_flow import router as work_flow_router
from backend.api.v1.unified_chat import router as unified_chat_router
from backend.api.v1.knowledge import router as knowledge_router

api_router = APIRouter(prefix="/api/v1")

# 挂载子路由
api_router.include_router(auth_router,         tags=["认证"])
api_router.include_router(assessment_router,   tags=["评估"])
api_router.include_router(sop_qa_router,       tags=["作业SOP问答"])
api_router.include_router(alert_router,        tags=["告警研判"])
api_router.include_router(work_flow_router,    tags=["作业流程"])
api_router.include_router(unified_chat_router, tags=["统一对话"])
api_router.include_router(knowledge_router,   tags=["作业规范知识库检索"])
api_router.include_router(devices_router, tags=["IoT设备"])
