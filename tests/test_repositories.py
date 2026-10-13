# tests/test_repositories.py
# 数据访问层读写回归（依赖 PG；写入的数据用例结束后清理）

import asyncio

import pytest
from sqlalchemy import text

from tests.conftest import make_id

from backend.db import repositories as repo
from backend.dependencies import AsyncSessionLocal


@pytest.fixture(autouse=True)
async def _cleanup(db_ready):
    """每个用例结束后清理 pytest- 前缀的测试数据。"""
    yield
    async with AsyncSessionLocal() as session:
        for table, key in [
            ("dispatch_orders", "order_id"),
            ("alerts", "alert_id"),
            ("assessment_results", "assessment_id"),
            ("care_flow_reports", "session_id"),
            ("care_flow_sessions", "session_id"),
            ("users", "user_id"),
            ("care_plans", "plan_id"),
            ("care_records", "record_id"),
            ("devices", "device_id"),
            ("elders", "elder_id"),
        ]:
            await session.execute(text(
                f"DELETE FROM {table} WHERE {key} LIKE 'pytest-%'"))
        await session.commit()


@pytest.mark.asyncio
async def test_assessment_roundtrip():
    asmt_id = make_id("asmt")
    scores = [{"dimension": "adl", "score": 70, "issues": ["行动迟缓"]}]
    summary = {"summary": "总体尚可", "care_level": "介助"}

    assert await repo.save_assessment_result(
        asmt_id, make_id("elder"), scores, 70.0, summary) is True

    row = await repo.get_assessment_result(asmt_id)
    assert row is not None
    assert float(row["weighted_score"]) == 70.0
    assert row["dimension_scores"][0]["dimension"] == "adl"
    assert row["summary"]["care_level"] == "介助"


@pytest.mark.asyncio
async def test_assessment_not_found():
    assert await repo.get_assessment_result(make_id("none")) is None


@pytest.mark.asyncio
async def test_care_plan_roundtrip():
    """评估派生计划写入 → 按 elder_id 读回最新版本。"""
    elder_id = make_id("elder")
    plan_id = make_id("plan")
    content = {
        "plan_id": plan_id,
        "elder_id": elder_id,
        "care_level": "介护",
        "daily_tasks": ["[HIGH] 鼻饲管护理", "[MEDIUM] 每2小时翻身"],
        "special_notes": "卧床，重点防压疮",
    }

    assert await repo.save_care_plan(plan_id, elder_id, content) is True

    row = await repo.get_latest_care_plan(elder_id)
    assert row is not None
    assert row["plan_id"] == plan_id
    assert row["plan_content"]["care_level"] == "介护"
    assert len(row["plan_content"]["daily_tasks"]) == 2


@pytest.mark.asyncio
async def test_care_plan_idempotent_update():
    """同一 plan_id 重放走 ON CONFLICT 更新，不新增行。"""
    elder_id = make_id("elder")
    plan_id = make_id("plan")

    assert await repo.save_care_plan(
        plan_id, elder_id, {"care_level": "介助", "daily_tasks": []}) is True
    assert await repo.save_care_plan(
        plan_id, elder_id, {"care_level": "介护", "daily_tasks": ["翻身"]}) is True

    async with AsyncSessionLocal() as session:
        n = (await session.execute(text(
            "SELECT count(*) FROM care_plans WHERE plan_id = :pid"),
            {"pid": plan_id})).scalar()
    assert n == 1
    row = await repo.get_latest_care_plan(elder_id)
    assert row["plan_content"]["care_level"] == "介护"


@pytest.mark.asyncio
async def test_care_plan_latest_version():
    """同一老人多份计划时，created_at 最新的胜出（阶段复评版本链）。"""
    elder_id = make_id("elder")
    await repo.save_care_plan(make_id("plan-v1"), elder_id,
                              {"care_level": "介助", "daily_tasks": []})
    await repo.save_care_plan(make_id("plan-v2"), elder_id,
                              {"care_level": "介护", "daily_tasks": []})

    row = await repo.get_latest_care_plan(elder_id)
    assert row is not None
    # 两条都在，latest 取最近写入的 v2
    assert row["plan_content"]["care_level"] == "介护"


@pytest.mark.asyncio
async def test_care_plan_not_found():
    assert await repo.get_latest_care_plan(make_id("none")) is None


@pytest.mark.asyncio
async def test_care_flow_report_roundtrip():
    session_id = make_id("session")
    report = {"overall_score": 80, "dimensions": [
        {"dimension": "照护规范性", "score": 80, "comment": "ok"}]}

    assert await repo.save_care_flow_report(
        session_id=session_id, elder_id=make_id("elder"),
        care_plan_id="", stage_reached="finished",
        total_turns=8, report=report) is True

    row = await repo.get_care_flow_report(session_id)
    assert row is not None
    assert row["stage_reached"] == "finished"
    assert row["report"]["overall_score"] == 80


# ── 照护流程会话登记（care_flow_sessions）────────────────────

@pytest.mark.asyncio
async def test_care_flow_session_roundtrip():
    """登记 → 查进行中 → 每轮同步进度 → finished 后不再查到。"""
    elder_id = make_id("elder")
    session_id = make_id("cf")

    assert await repo.create_care_flow_session(
        session_id, elder_id, make_id("staff"), "admission_assessment") is True

    # /chat 依赖按 session_id 找回 elder_id，保证 thread_id 与 /start 一致
    lookup = await repo.get_care_flow_session(session_id)
    assert lookup is not None
    assert lookup["elder_id"] == elder_id
    assert lookup["status"] == "in_progress"

    row = await repo.get_active_care_flow_session(elder_id)
    assert row is not None
    assert row["session_id"] == session_id
    assert row["current_stage"] == "admission_assessment"
    assert row["total_turns"] == 0

    assert await repo.update_care_flow_session_progress(
        session_id, "daily_care", 5) is True
    row = await repo.get_active_care_flow_session(elder_id)
    assert row is not None
    assert row["current_stage"] == "daily_care"
    assert row["total_turns"] == 5

    # 走到 finished：状态置完成，"进行中"查询不再返回
    assert await repo.update_care_flow_session_progress(
        session_id, "finished", 12) is True
    assert await repo.get_active_care_flow_session(elder_id) is None


@pytest.mark.asyncio
async def test_care_flow_session_create_idempotent():
    """同 session_id 重复登记走 ON CONFLICT DO NOTHING，保留首次数据。"""
    elder_id = make_id("elder")
    session_id = make_id("cf")

    assert await repo.create_care_flow_session(
        session_id, elder_id, make_id("staff"), "admission_assessment") is True
    assert await repo.create_care_flow_session(
        session_id, elder_id, make_id("staff"), "daily_care") is True

    row = await repo.get_active_care_flow_session(elder_id)
    assert row is not None
    assert row["current_stage"] == "admission_assessment"


@pytest.mark.asyncio
async def test_care_flow_session_active_none_when_missing():
    assert await repo.get_active_care_flow_session(make_id("elder")) is None


@pytest.mark.asyncio
async def test_care_flow_session_latest_wins():
    """同一老人多场进行中会话，取最近发起的一场。"""
    elder_id = make_id("elder")
    s1, s2 = make_id("cf"), make_id("cf")

    assert await repo.create_care_flow_session(
        s1, elder_id, make_id("staff"), "admission_assessment") is True
    await asyncio.sleep(0.01)  # 保证 started_at 时间戳可区分
    assert await repo.create_care_flow_session(
        s2, elder_id, make_id("staff"), "daily_care") is True

    row = await repo.get_active_care_flow_session(elder_id)
    assert row is not None
    assert row["session_id"] == s2


@pytest.mark.asyncio
async def test_alert_roundtrip():
    alert_id = make_id("alert")
    elder_id = make_id("elder")
    assessment = {"risk_level": "high", "risk_score": 85}
    tracks = {"device_readings": [], "care_records": [], "health_indicators": []}

    assert await repo.save_alert_triggered(
        alert_id, elder_id, make_id("dev"), "fall_detected",
        assessment, tracks) is True

    row = await repo.get_alert_review(alert_id)
    assert row is not None
    assert row["status"] == "reviewing"
    assert row["risk_assessment"]["risk_level"] == "high"

    # 派发工单 → 状态更新为 dispatched
    assert await repo.save_alert_dispatched(alert_id, elder_id, {
        "order_id": make_id("order"),
        "status": "dispatched",
        "order_content": "# 测试工单\n立即上门",
    }) is True

    row = await repo.get_alert_review(alert_id)
    assert row["status"] == "dispatched"


@pytest.mark.asyncio
async def test_ensure_default_users_idempotent(db_ready):
    """重复执行播种不报错、不产生重复行。"""
    await repo.ensure_default_users()
    await repo.ensure_default_users()
    async with AsyncSessionLocal() as session:
        n = (await session.execute(text("SELECT count(*) FROM users"))).scalar()
        assert n >= 3
