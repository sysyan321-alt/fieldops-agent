# tests/test_work_flow_webhook.py
# 出站通知（Agent → SaaS）：签名 / 投递 / 重试退避 / 跳过未配置 / 发件箱幂等。
#
# 单元用例用 fake repo + fake httpx，不依赖 DB / 网络；
# 末尾几条集成用例走 db_ready（PG 不可用时自动跳过）。

import hashlib
import hmac
from types import SimpleNamespace

import pytest
from sqlalchemy import text

from tests.conftest import make_id

from backend.core import outbound as outbound_mod
from backend.db import repositories as repo
from backend.dependencies import AsyncSessionLocal

TENANT = "tenant_default"
HOOK_URL = "https://saas.example.com/hooks/fieldops-agent"


# ── 测试替身 ─────────────────────────────────────────────────

def _event(**overrides) -> dict:
    base = {
        "event_id": "evt-test-1",
        "tenant_id": TENANT,
        "event_type": outbound_mod.EVENT_CARE_FLOW_FINISHED,
        "aggregate_id": "cf-test-1",
        "payload": {"event": "work_flow.finished", "session_id": "cf-test-1"},
        "status": "pending",
        "attempts": 0,
        "last_error": "",
        "next_retry_at": None,
        "created_at": None,
        "sent_at": None,
    }
    base.update(overrides)
    return base


def _webhook(**overrides) -> dict:
    base = {"tenant_id": TENANT, "url": HOOK_URL, "secret": "s3cr3t", "enabled": True}
    base.update(overrides)
    return base


class _FakeRepo:
    """替代 backend.db.repositories 的最小替身，记录投递后的状态迁移。"""

    def __init__(self, events=None, webhook=None):
        self.events = list(events or [])
        self.webhook = webhook
        self.sent: list[str] = []
        self.retried: list[dict] = []
        self.failed: list[tuple[str, str]] = []
        self.enqueue_calls: list[dict] = []

    async def list_pending_outbound_events(self, limit=20):
        return self.events[:limit]

    async def get_tenant_webhook(self, tenant_id):
        return self.webhook

    async def mark_outbound_event_sent(self, event_id):
        self.sent.append(event_id)
        return True

    async def mark_outbound_event_retry(self, event_id, error, delay_seconds):
        self.retried.append(
            {"event_id": event_id, "delay": delay_seconds, "error": error})
        return True

    async def mark_outbound_event_failed(self, event_id, error):
        self.failed.append((event_id, error))
        return True

    async def enqueue_outbound_event(self, **kwargs):
        self.enqueue_calls.append(kwargs)
        return "evt-enqueued"


class _FakeResponse:
    def __init__(self, status_code=200, text_=""):
        self.status_code = status_code
        self.text = text_


class _FakeAsyncClient:
    """替代 httpx.AsyncClient：记录请求，返回预设响应（或抛错）。"""

    response = _FakeResponse(200)
    raise_exc: Exception | None = None
    captured: dict = {}

    def __init__(self, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def post(self, url, content=None, headers=None):
        _FakeAsyncClient.captured = {"url": url, "content": content, "headers": headers}
        if _FakeAsyncClient.raise_exc is not None:
            raise _FakeAsyncClient.raise_exc
        return _FakeAsyncClient.response


@pytest.fixture(autouse=True)
def _reset_fake_http():
    _FakeAsyncClient.response = _FakeResponse(200)
    _FakeAsyncClient.raise_exc = None
    _FakeAsyncClient.captured = {}
    yield


def _patch(monkeypatch, fake_repo):
    monkeypatch.setattr(outbound_mod, "repo", fake_repo)
    monkeypatch.setattr(
        outbound_mod, "httpx",
        SimpleNamespace(AsyncClient=_FakeAsyncClient),
    )
    return fake_repo


# ── 签名与请求头 ─────────────────────────────────────────────

def test_sign_payload_matches_hmac_reference():
    """签名即 HMAC-SHA256 十六进制；secret 为空则返回空串（SaaS 侧跳过校验）。"""
    body = b'{"hello":"world"}'
    assert outbound_mod.sign_payload("s3cr3t", body) == hmac.new(
        b"s3cr3t", body, hashlib.sha256).hexdigest()
    assert outbound_mod.sign_payload("", body) == ""


def test_build_headers_carries_event_id_and_signature():
    body = b"{}"
    headers = outbound_mod.build_headers(_event(attempts=2), body, "s3cr3t")
    assert headers["X-FieldOps-Event"] == outbound_mod.EVENT_CARE_FLOW_FINISHED
    assert headers["X-FieldOps-Event-Id"] == "evt-test-1"
    assert headers["X-FieldOps-Delivery-Attempt"] == "3"   # 投递前 attempts=2 → 本次是第 3 次
    assert headers["X-FieldOps-Signature"].startswith("sha256=")
    assert "X-FieldOps-Timestamp" in headers


def test_build_headers_omits_signature_without_secret():
    headers = outbound_mod.build_headers(_event(), b"{}", "")
    assert "X-FieldOps-Signature" not in headers


# ── 投递与重试策略 ───────────────────────────────────────────

@pytest.mark.asyncio
async def test_dispatch_sends_and_marks_sent(monkeypatch):
    """2xx → 标记 sent，不会进入重试。"""
    fake = _patch(monkeypatch, _FakeRepo(events=[_event()], webhook=_webhook()))
    _FakeAsyncClient.response = _FakeResponse(200)

    stats = await outbound_mod.dispatch_pending()

    assert stats == {"scanned": 1, "sent": 1, "retried": 0, "failed": 0, "skipped": 0}
    assert fake.sent == ["evt-test-1"]
    assert _FakeAsyncClient.captured["url"] == HOOK_URL
    # 载荷是事件 payload 本身，不是整个事件行
    assert b"work_flow.finished" in _FakeAsyncClient.captured["content"]


@pytest.mark.asyncio
async def test_dispatch_retries_with_backoff_on_5xx(monkeypatch):
    """5xx → 按第一次退避 30s 排重试，不置 failed。"""
    fake = _patch(monkeypatch, _FakeRepo(events=[_event(attempts=0)], webhook=_webhook()))
    _FakeAsyncClient.response = _FakeResponse(503, "upstream down")

    stats = await outbound_mod.dispatch_pending()

    assert stats["retried"] == 1 and stats["failed"] == 0
    assert fake.retried[0]["event_id"] == "evt-test-1"
    assert fake.retried[0]["delay"] == 30        # 第一次失败：等 30s
    assert "503" in fake.retried[0]["error"]     # 失败原因写进 last_error
    assert not fake.sent


@pytest.mark.asyncio
async def test_dispatch_backoff_grows_with_attempts(monkeypatch):
    """退避随尝试次数增长：attempts=1 → 120s；attempts=2 → 600s。"""
    _patch(monkeypatch, _FakeRepo(events=[_event(attempts=1)], webhook=_webhook()))
    _FakeAsyncClient.response = _FakeResponse(500, "boom")
    await outbound_mod.dispatch_pending()
    assert outbound_mod.repo.retried[0]["delay"] == 120

    _patch(monkeypatch, _FakeRepo(events=[_event(attempts=2)], webhook=_webhook()))
    _FakeAsyncClient.response = _FakeResponse(500, "boom")
    await outbound_mod.dispatch_pending()
    assert outbound_mod.repo.retried[0]["delay"] == 600


@pytest.mark.asyncio
async def test_dispatch_marks_failed_after_max_attempts(monkeypatch):
    """最后一次尝试仍失败 → 置 failed（保留记录供人工重投）。"""
    fake = _patch(monkeypatch, _FakeRepo(
        events=[_event(attempts=outbound_mod.MAX_ATTEMPTS - 1)], webhook=_webhook()))
    _FakeAsyncClient.response = _FakeResponse(500, "still down")

    stats = await outbound_mod.dispatch_pending()

    assert stats["failed"] == 1 and stats["retried"] == 0
    assert fake.failed[0][0] == "evt-test-1"


@pytest.mark.asyncio
async def test_dispatch_treats_network_error_as_failure(monkeypatch):
    """连接异常（超时 / DNS）也算投递失败，走退避而不是把流程打崩。"""
    fake = _patch(monkeypatch, _FakeRepo(events=[_event()], webhook=_webhook()))
    _FakeAsyncClient.raise_exc = ConnectionError("dns failure")

    stats = await outbound_mod.dispatch_pending()

    assert stats["retried"] == 1
    assert fake.retried[0]["delay"] == 30
    assert "ConnectionError" in fake.retried[0]["error"]


@pytest.mark.asyncio
async def test_dispatch_skips_tenant_without_webhook(monkeypatch):
    """未配回调地址：事件保留 pending、不计尝试次数，等配置好后自动补投。"""
    fake = _patch(monkeypatch, _FakeRepo(events=[_event()], webhook=None))

    stats = await outbound_mod.dispatch_pending()

    assert stats["skipped"] == 1
    assert not fake.sent and not fake.retried and not fake.failed
    assert _FakeAsyncClient.captured == {}   # 压根没发请求


@pytest.mark.asyncio
async def test_dispatch_skips_disabled_webhook(monkeypatch):
    """回调被停用：同样保留 pending，不发请求。"""
    fake = _patch(monkeypatch, _FakeRepo(
        events=[_event()], webhook=_webhook(enabled=False)))
    stats = await outbound_mod.dispatch_pending()
    assert stats["skipped"] == 1
    assert not fake.sent and not fake.retried and not fake.failed


# ── 事件构造 ─────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_enqueue_work_flow_finished_builds_payload(monkeypatch):
    """终态事件载荷含报告与回查地址，且带上是否由退住工单驱动。"""
    fake = _patch(monkeypatch, _FakeRepo())

    event_id = await outbound_mod.enqueue_work_flow_finished(
        session_id="cf-abc",
        site_id="s001",
        tenant_id=TENANT,
        service_plan_id="plan-1",
        stage_reached="finished",
        total_turns=18,
        report={"overall_score": 88},
        discharge_driven=True,
    )

    assert event_id == "evt-enqueued"
    call = fake.enqueue_calls[0]
    assert call["event_type"] == outbound_mod.EVENT_CARE_FLOW_FINISHED
    assert call["aggregate_id"] == "cf-abc"          # 幂等键就是 session_id
    payload = call["payload"]
    assert payload["report"] == {"overall_score": 88}
    assert payload["report_url"] == "/api/v1/work-flow/cf-abc/report"
    assert payload["discharge_driven"] is True
    assert payload["total_turns"] == 18


@pytest.mark.asyncio
async def test_enqueue_alert_dispatched_event(monkeypatch):
    """派单终态 → alert.dispatched 事件，幂等键取 order_id（每张工单唯一）。"""
    fake = _patch(monkeypatch, _FakeRepo())

    await outbound_mod.enqueue_alert_decision(
        alert_id="alert-1",
        site_id="s001",
        tenant_id=TENANT,
        dispatch_order={
            "order_id": "order-9",
            "risk_level": "high",
            "actions": ["立即翻身", "通知家属"],
            "order_content": "风险等级：high",
            "supervisor_id": "sup-1",
            "status": "dispatched",
        },
        device_id="dev-1",
        alert_type="fall",
    )

    call = fake.enqueue_calls[0]
    assert call["event_type"] == outbound_mod.EVENT_ALERT_DISPATCHED
    assert call["aggregate_id"] == "order-9"
    assert call["payload"]["alert_id"] == "alert-1"
    assert call["payload"]["device_id"] == "dev-1"
    assert call["payload"]["actions"] == ["立即翻身", "通知家属"]
    assert call["payload"]["status"] == "dispatched"


@pytest.mark.asyncio
async def test_enqueue_alert_dismissed_event(monkeypatch):
    """站长取消处置 → alert.dismissed（虽不派单，仍要通知业务方"已取消"）。"""
    fake = _patch(monkeypatch, _FakeRepo())

    await outbound_mod.enqueue_alert_decision(
        alert_id="alert-2",
        site_id="s002",
        tenant_id=TENANT,
        dispatch_order={"order_id": "order-10", "status": "dismissed"},
    )

    assert fake.enqueue_calls[0]["event_type"] == outbound_mod.EVENT_ALERT_DISMISSED
    assert fake.enqueue_calls[0]["aggregate_id"] == "order-10"


@pytest.mark.asyncio
async def test_enqueue_alert_falls_back_to_alert_id_without_order(monkeypatch):
    """工单缺 order_id 时幂等键退回 alert_id（不让事件因缺字段丢失）。"""
    fake = _patch(monkeypatch, _FakeRepo())

    await outbound_mod.enqueue_alert_decision(
        alert_id="alert-3", site_id="s003", tenant_id=TENANT,
        dispatch_order={"status": "dispatched"},
    )

    assert fake.enqueue_calls[0]["aggregate_id"] == "alert-3"


# ── 集成：发件箱幂等 + 回调配置回读（依赖 PG） ────────────────

@pytest.fixture
async def _cleanup(db_ready):
    yield
    async with AsyncSessionLocal() as session:
        await session.execute(text(
            "DELETE FROM outbound_events WHERE aggregate_id LIKE 'pytest-%'"))
        await session.execute(text(
            "DELETE FROM tenant_webhooks WHERE tenant_id LIKE 'pytest-%'"))
        await session.commit()


@pytest.mark.asyncio
async def test_outbound_event_enqueue_is_idempotent(_cleanup):
    """同一 session 落两次终态事件：只留一条（发件箱唯一索引兜底）。"""
    session_id = make_id("cf")
    tenant_id = make_id("tenant")

    first = await repo.enqueue_outbound_event(
        event_type="work_flow.finished", aggregate_id=session_id,
        payload={"session_id": session_id}, tenant_id=tenant_id)
    second = await repo.enqueue_outbound_event(
        event_type="work_flow.finished", aggregate_id=session_id,
        payload={"session_id": session_id}, tenant_id=tenant_id)

    assert first is not None
    assert second is None                     # 重复落库被吃掉

    rows = await repo.list_outbound_events(tenant_id=tenant_id)
    assert len(rows) == 1
    assert rows[0]["status"] == "pending"
    assert rows[0]["attempts"] == 0


@pytest.mark.asyncio
async def test_tenant_webhook_roundtrip_and_redelivery(_cleanup):
    """回调配置覆盖写可回读；失败事件能被重置为 pending 重投。"""
    tenant_id = make_id("tenant")
    session_id = make_id("cf")

    assert await repo.upsert_tenant_webhook(tenant_id, "https://a.example.com/hook",
                                            "k1", True) is True
    assert await repo.upsert_tenant_webhook(tenant_id, "https://b.example.com/hook",
                                            "k2", False) is True

    hook = await repo.get_tenant_webhook(tenant_id)
    assert hook["url"] == "https://b.example.com/hook"   # 覆盖写生效
    assert hook["secret"] == "k2"
    assert hook["enabled"] is False

    event_id = await repo.enqueue_outbound_event(
        event_type="work_flow.finished", aggregate_id=session_id,
        payload={"session_id": session_id}, tenant_id=tenant_id)
    assert await repo.mark_outbound_event_failed(event_id, "HTTP 500") is True

    row = await repo.get_outbound_event(event_id, tenant_id=tenant_id)
    assert row["status"] == "failed" and row["attempts"] == 1

    assert await repo.reset_outbound_event_for_redelivery(
        event_id, tenant_id=tenant_id) is True
    row = await repo.get_outbound_event(event_id, tenant_id=tenant_id)
    assert row["status"] == "pending" and row["attempts"] == 0
    assert row["last_error"] == "" and row["sent_at"] is None
