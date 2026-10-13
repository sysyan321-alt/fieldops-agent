# tests/test_tenant_isolation.py
# 多租户强制隔离测试：跨租户读不可见（视为不存在）、跨租户写被拒绝

import pytest
from sqlalchemy import text as sql_text

from tests.conftest import make_id

from backend.db import repositories as repo
from backend.db import iot_repo
from backend.dependencies import AsyncSessionLocal, pwd_context

TENANT_A = "pytest-tenant-a"
TENANT_B = "pytest-tenant-b"


@pytest.fixture(autouse=True)
async def _cleanup(db_ready):
    """每个用例结束后清理 pytest- 前缀与测试租户的数据。"""
    yield
    async with AsyncSessionLocal() as session:
        # 先删子表（device_readings 引用 devices），再删主表
        await session.execute(sql_text(
            "DELETE FROM device_readings WHERE site_id LIKE 'pytest-%'"))
        for table, key in [
            ("dispatch_orders", "order_id"),
            ("alerts", "alert_id"),
            ("assessment_results", "assessment_id"),
            ("work_flow_reports", "session_id"),
            ("devices", "device_id"),
            ("sites", "site_id"),
        ]:
            await session.execute(sql_text(
                f"DELETE FROM {table} WHERE {key} LIKE 'pytest-%'"))
        await session.execute(sql_text(
            "DELETE FROM users WHERE tenant_id IN (:a, :b)"),
            {"a": TENANT_A, "b": TENANT_B})
        await session.commit()


# ── 仓储层：读隔离 ──────────────────────────────────────────

@pytest.mark.asyncio
async def test_assessment_cross_tenant_read_blocked():
    asmt_id = make_id("asmt")
    site_a = make_id("site")

    assert await repo.save_assessment_result(
        asmt_id, site_a, [{"dimension": "adl", "score": 80, "issues": []}],
        80.0, {"summary": {}}, tenant_id=TENANT_A) is True

    # 本租户可读
    row = await repo.get_assessment_result(asmt_id, tenant_id=TENANT_A)
    assert row is not None
    assert float(row["weighted_score"]) == 80.0

    # 跨租户读 → 视为不存在
    assert await repo.get_assessment_result(asmt_id, tenant_id=TENANT_B) is None


@pytest.mark.asyncio
async def test_work_flow_report_cross_tenant_read_blocked():
    session_id = make_id("session")

    assert await repo.save_work_flow_report(
        session_id=session_id, site_id=make_id("site"), service_plan_id="",
        stage_reached="finished", total_turns=5, report={"score": 90},
        tenant_id=TENANT_A) is True

    assert await repo.get_work_flow_report(session_id, tenant_id=TENANT_A) is not None
    assert await repo.get_work_flow_report(session_id, tenant_id=TENANT_B) is None


@pytest.mark.asyncio
async def test_alert_cross_tenant_read_and_dispatch_blocked():
    alert_id = make_id("alert")
    site_a = make_id("site")

    assert await repo.save_alert_triggered(
        alert_id, site_a, make_id("dev"), "fall_detected",
        {"risk_level": "high"}, {}, tenant_id=TENANT_A) is True

    # 本租户可读
    assert await repo.get_alert_review(alert_id, tenant_id=TENANT_A) is not None
    # 跨租户读 → 视为不存在
    assert await repo.get_alert_review(alert_id, tenant_id=TENANT_B) is None

    # 跨租户派发（更新告警状态）→ 拒绝
    assert await repo.save_alert_dispatched(
        alert_id, site_a, {"order_id": make_id("order"),
                            "status": "dispatched", "order_content": "x"},
        tenant_id=TENANT_B) is False
    # 状态未被跨租户篡改
    row = await repo.get_alert_review(alert_id, tenant_id=TENANT_A)
    assert row["status"] == "reviewing"

    # 本租户派发 → 成功
    assert await repo.save_alert_dispatched(
        alert_id, site_a, {"order_id": make_id("order"),
                            "status": "dispatched", "order_content": "x"},
        tenant_id=TENANT_A) is True


# ── 仓储层：写隔离（老人归属守卫）────────────────────────────

@pytest.mark.asyncio
async def test_cross_tenant_write_rejected():
    site_a = make_id("site")
    asmt_id = make_id("asmt")

    # 租户 A 创建老人（占位行归属 A）
    assert await repo._ensure_site(site_a, tenant_id=TENANT_A) == TENANT_A

    # 租户 B 对 A 的老人写入评估 → 拒绝，且不落库
    assert await repo.save_assessment_result(
        asmt_id, site_a, [], 50.0, {}, tenant_id=TENANT_B) is False
    assert await repo.get_assessment_result(asmt_id, tenant_id=TENANT_B) is None
    assert await repo.get_assessment_result(asmt_id, tenant_id=TENANT_A) is None

    # 告警同理：跨租户触发 → 拒绝
    assert await repo.save_alert_triggered(
        make_id("alert"), site_a, make_id("dev"), "fall_detected",
        {}, {}, tenant_id=TENANT_B) is False


# ── IoT 层：设备读数租户过滤 ─────────────────────────────────

@pytest.mark.asyncio
async def test_device_readings_tenant_filter():
    site_a = make_id("site")
    device_a = make_id("dev")
    await repo._ensure_site(site_a, tenant_id=TENANT_A)
    await repo._ensure_device(device_a, site_a, TENANT_A)

    readings = [{"metric_name": "心率", "reading_value": "120",
                 "reading_unit": "bpm", "is_abnormal": True}]
    assert await iot_repo.save_device_readings(
        site_a, device_a, "心率手环", readings, tenant_id=TENANT_A) == 1

    # 本租户可查；跨租户查 → 空
    assert len(await iot_repo.get_recent_device_readings(
        site_a, limit=10, tenant_id=TENANT_A)) == 1
    assert await iot_repo.get_recent_device_readings(
        site_a, limit=10, tenant_id=TENANT_B) == []
    assert await iot_repo.get_recent_health_indicators(
        site_a, limit=8, tenant_id=TENANT_B) == []


# ── API 层：JWT 租户贯通 ────────────────────────────────────

@pytest.mark.asyncio
async def test_api_cross_tenant_404():
    """租户 B 的站长访问租户 A 的告警 → 404（不泄露存在性）。"""
    import httpx
    from backend.main import app

    alert_id = make_id("alert")
    assert await repo.save_alert_triggered(
        alert_id, make_id("site"), make_id("dev"), "fall_detected",
        {"risk_level": "high"}, {}, tenant_id=TENANT_A) is True

    # 造一个租户 B 的站长账号
    username = make_id("sup-b").replace("pytest-", "supb-")
    async with AsyncSessionLocal() as session:
        await session.execute(sql_text(
            "INSERT INTO users (user_id, username, password_hash, role, tenant_id)"
            " VALUES (:uid, :uname, :hash, 'supervisor', :tenant)"
            " ON CONFLICT (username) DO NOTHING"), {
            "uid": make_id("u"), "uname": username,
            "hash": pwd_context.hash("Pass@123"), "tenant": TENANT_B})
        await session.commit()

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post("/api/v1/auth/login", json={
            "username": username, "password": "Pass@123"})
        assert resp.status_code == 200
        headers = {"Authorization": f"Bearer {resp.json()['access_token']}"}

        # 跨租户读告警 → 404
        resp = await client.get(f"/api/v1/alert/{alert_id}/review", headers=headers)
        assert resp.status_code == 404

        # 跨租户读数 → 空列表
        resp = await client.get(
            f"/api/v1/devices/site/{make_id('none')}/readings", headers=headers)
        assert resp.status_code == 200
        assert resp.json()["readings"] == []

        # 跨租户上报读数给 A 的老人 → 403
        site_a = await _first_site_of_tenant(TENANT_A)
        resp = await client.post(
            f"/api/v1/devices/{make_id('dev')}/readings", headers=headers,
            json={"site_id": site_a, "device_type": "心率手环",
                  "readings": [{"metric_name": "心率", "reading_value": "120"}]})
        assert resp.status_code == 403


async def _first_site_of_tenant(tenant_id: str) -> str:
    async with AsyncSessionLocal() as session:
        return (await session.execute(sql_text(
            "SELECT site_id FROM sites WHERE tenant_id = :t AND site_id LIKE 'pytest-%'"
            " LIMIT 1"), {"t": tenant_id})).scalar()
