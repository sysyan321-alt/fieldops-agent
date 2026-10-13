# tests/test_export_and_search.py
# P1 新增能力测试：护理 SOP 语义搜索接口 + 评估/照护报告 Word 导出

import io
import zipfile

import pytest
import httpx

from backend.main import app
from tests.conftest import make_id


@pytest.fixture
async def client():
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest.fixture
async def auth_headers(client, db_ready):
    resp = await client.post("/api/v1/auth/login", json={
        "username": "admin", "password": "Admin@123"})
    assert resp.status_code == 200, f"登录失败: {resp.text}"
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


# ── 语义搜索接口 ────────────────────────────────────────────────

async def test_knowledge_search_requires_auth(client):
    """未带 token → 401。"""
    resp = await client.post("/api/v1/knowledge/search", json={"query": "鼻饲管护理"})
    assert resp.status_code == 401


async def test_knowledge_search_shape(client, auth_headers, monkeypatch):
    """检索链路用桩替换（避免测试依赖 BGE 模型与 Milvus），校验返回契约。"""
    from backend.core.reranker import RankedDocument

    async def fake_retrieve(query, tenant_id, course_id=None, recall_top_k=10, rerank_top_k=3):
        return (
            [
                RankedDocument(content="喂养前需检查管路位置与胃残留量。", score=0.93,
                               original_index=0,
                               metadata={"source_name": "鼻饲管护理操作规范", "course_id": "feeding_tube"}),
                RankedDocument(content="每日进行口腔与鼻部清洁。", score=0.62,
                               original_index=1,
                               metadata={"source_name": "鼻饲管护理操作规范", "course_id": "feeding_tube"}),
            ],
            0.93,
        )

    monkeypatch.setattr("backend.core.retrieval_cache.cached_retrieve", fake_retrieve)

    resp = await client.post("/api/v1/knowledge/search",
                             json={"query": "鼻饲管日常护理", "top_k": 2},
                             headers=auth_headers)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["count"] == 2
    assert body["is_high_confidence"] is True
    assert body["confidence"] == 0.93
    assert body["hits"][0]["source_name"] == "鼻饲管护理操作规范"

    # min_confidence 过滤低于阈值的结果
    resp2 = await client.post("/api/v1/knowledge/search",
                              json={"query": "鼻饲管日常护理", "min_confidence": 0.8},
                              headers=auth_headers)
    assert resp2.json()["count"] == 1


# ── 报告导出 ───────────────────────────────────────────────────

def _is_docx(payload: bytes) -> bool:
    """docx 是 zip 容器：校验魔数并能读出 word/document.xml。"""
    if not payload.startswith(b"PK"):
        return False
    with zipfile.ZipFile(io.BytesIO(payload)) as zf:
        return "word/document.xml" in zf.namelist()


async def test_assessment_export_docx(client, auth_headers):
    """写入一条评估结果 → 导出 Word 成功，且内容里能看到关键字段。"""
    from backend.db.repositories import save_assessment_result

    assessment_id = make_id("export-asmt")
    await save_assessment_result(
        assessment_id=assessment_id,
        site_id="e001",
        dimension_scores=[
            {"dimension": "ADL 日常生活能力", "score": 60, "weight": 0.30},
            {"dimension": "认知功能", "score": 70, "weight": 0.25},
        ],
        weighted_score=64.5,
        summary_payload={
            "summary": {"overall_comment": "整体三级能力中等，需重点防跌倒。",
                        "risk_tier_recommendation": "二级"},
            "issues": [{"priority": "high", "description": "跌倒风险偏高",
                        "suggestion": "加装床栏并加强夜间巡视"}],
            "structured": {"name": "张明德", "built_year": 78, "risk_tier": "二级"},
        },
    )

    resp = await client.get(f"/api/v1/assessment/{assessment_id}/export", headers=auth_headers)
    assert resp.status_code == 200, resp.text
    assert resp.headers["content-type"].startswith(
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document")
    assert "attachment" in resp.headers["content-disposition"]
    assert _is_docx(resp.content)

    with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
        xml = zf.read("word/document.xml").decode("utf-8")
    assert "老年人能力评估报告" in xml
    assert "跌倒风险偏高" in xml
    assert "二级" in xml


async def test_assessment_export_404(client, auth_headers):
    resp = await client.get(f"/api/v1/assessment/{make_id('none')}/export", headers=auth_headers)
    assert resp.status_code == 404


async def test_work_flow_export_docx(client, auth_headers):
    """写入一条照护报告 → 导出 Word 成功。"""
    from backend.db.repositories import save_work_flow_report

    session_id = make_id("export-flow")
    await save_work_flow_report(
        session_id=session_id,
        site_id="e001",
        service_plan_id="plan-001",
        stage_reached="finished",
        total_turns=12,
        report={"content": "照护流程执行完毕，老人状态稳定。",
                "recommendation": "维持当前照护等级"},
    )

    resp = await client.get(f"/api/v1/work-flow/{session_id}/report/export",
                            headers=auth_headers)
    assert resp.status_code == 200, resp.text
    assert _is_docx(resp.content)

    with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
        xml = zf.read("word/document.xml").decode("utf-8")
    assert "照护流程报告" in xml
    assert "照护流程执行完毕" in xml


async def test_work_flow_export_404(client, auth_headers):
    resp = await client.get(f"/api/v1/work-flow/{make_id('none')}/report/export",
                            headers=auth_headers)
    assert resp.status_code == 404
