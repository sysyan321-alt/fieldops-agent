# backend/api/v1/assessment.py
# 评估 Agent API 路由

import uuid
import os
import tempfile

from fastapi import APIRouter, Depends, UploadFile, File, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel

from backend.dependencies import get_current_user
from backend.core.orchestrator import get_orchestrator, AgentRequest

router = APIRouter(prefix="/assessment")


class AssessmentResponse(BaseModel):
    """评估结果响应。"""
    assessment_id: str
    status: str
    weighted_score: float = 0
    summary: dict = {}
    dimension_scores: list = []
    issues: list = []


@router.post("/upload", response_model=AssessmentResponse)
async def upload_assessment(
    site_id: str,
    file: UploadFile = File(...),
    current_user: dict = Depends(get_current_user),
):
    """上传评估表 PDF，触发评估流程。"""
    if not file.filename.lower().endswith(".pdf"):
        raise HTTPException(status_code=400, detail="仅支持 PDF 文件")

    # 保存临时文件
    assessment_id = f"asmt-{uuid.uuid4().hex[:12]}"
    suffix = os.path.splitext(file.filename)[1]
    with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
        content = await file.read()
        tmp.write(content)
        tmp_path = tmp.name

    try:
        orchestrator = get_orchestrator()
        request = AgentRequest(
            staff_id=current_user["user_id"],
            site_id=site_id,
            tenant_id=current_user["tenant_id"],
            session_id=assessment_id,
            agent_type="assessment",
            mode="single",
            context={"assessment_id": assessment_id, "pdf_local_path": tmp_path},
        )
        result = await orchestrator.execute(request)
        structured = result.get("structured_output", {})

        return AssessmentResponse(
            assessment_id=assessment_id,
            status="completed",
            weighted_score=structured.get("weighted_score", 0),
            summary=structured.get("summary", {}),
            dimension_scores=structured.get("dimension_scores", []),
            issues=structured.get("issues", []),
        )
    finally:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)


@router.get("/{assessment_id}", response_model=AssessmentResponse)
async def get_assessment(
    assessment_id: str,
    current_user: dict = Depends(get_current_user),
):
    """按评估 ID 查询历史评估结果（读 assessment_results 表，租户隔离）。"""
    from backend.db.repositories import get_assessment_result
    row = await get_assessment_result(assessment_id, tenant_id=current_user["tenant_id"])
    if row is None:
        raise HTTPException(status_code=404, detail=f"评估记录不存在: {assessment_id}")

    # summary 列存的是全量附加数据（structured/issues/summary）
    summary_payload = row.get("summary", {})
    return AssessmentResponse(
        assessment_id=row["assessment_id"],
        status="completed",
        weighted_score=row.get("weighted_score", 0),
        summary=summary_payload.get("summary", {}),
        dimension_scores=row.get("dimension_scores", []),
        issues=summary_payload.get("issues", []),
    )


@router.get("/{assessment_id}/export")
async def export_assessment_docx(
    assessment_id: str,
    current_user: dict = Depends(get_current_user),
):
    """把评估结果导出为 Word（.docx），便于打印、盖章与归档。"""
    from backend.core.export import build_assessment_docx, docx_response_headers
    from backend.db.repositories import get_assessment_result

    row = await get_assessment_result(assessment_id, tenant_id=current_user["tenant_id"])
    if row is None:
        raise HTTPException(status_code=404, detail=f"评估记录不存在: {assessment_id}")

    content = build_assessment_docx(row)
    filename = f"评估报告-{assessment_id}.docx"
    return Response(
        content=content,
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        headers=docx_response_headers(filename, ascii_name=f"assessment-report-{assessment_id}.docx"),
    )
