# tests/test_api.py
# API 集成测试：健康检查 + 登录 + 三个查询接口读库 + 404 分支

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


async def test_health(client):
    resp = await client.get("/health")
    assert resp.status_code == 200


async def test_query_endpoints_404(client, auth_headers):
    """三个查询接口：不存在的 ID → 404。"""
    for path in [
        f"/api/v1/assessment/{make_id('none')}",
        f"/api/v1/alert/{make_id('none')}/review",
        f"/api/v1/work-flow/{make_id('none')}/report",
    ]:
        resp = await client.get(path, headers=auth_headers)
        assert resp.status_code == 404, path


async def test_query_endpoints_unauthorized(client):
    """未带 token → 401。"""
    resp = await client.get("/api/v1/assessment/whatever")
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_query_endpoints_read_from_db(client, auth_headers):
    """写入一条评估记录 → 查询接口应能读到。"""
    from backend.db import repositories as repo

    asmt_id = make_id("asmt")
    assert await repo.save_assessment_result(
        asmt_id, make_id("site"),
        [{"dimension": "adl", "score": 60, "issues": []}],
        60.0, {"summary": {"risk_tier": "二级"}}) is True

    resp = await client.get(f"/api/v1/assessment/{asmt_id}", headers=auth_headers)
    assert resp.status_code == 200
    body = resp.json()
    assert body["assessment_id"] == asmt_id
    assert float(body["weighted_score"]) == 60.0
    assert body["summary"]["risk_tier"] == "二级"
