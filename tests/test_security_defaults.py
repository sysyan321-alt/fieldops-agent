# tests/test_security_defaults.py
# 开源前收尾的几条安全默认值：
#   ① CORS：通配来源 + allow_credentials 是无效组合，必须被拦住；
#   ② 登录失败限流：弱口令不能被脚本随便撞；
#   ③ /health 与 /health/ready 分工：存活探针便宜、就绪探针真探依赖；
#   ④ 演示账号默认不播种（不能一启动就带人人皆知的口令）。

import time

import pytest
from fastapi.middleware.cors import CORSMiddleware

from backend.config import Settings
from backend.core.login_guard import LoginGuard


# ── ① CORS ────────────────────────────────────────────────────

def _cors_kwargs(app):
    for mw in app.user_middleware:
        if mw.cls is CORSMiddleware:
            return mw.kwargs
    return None


def _build_app(monkeypatch, **overrides):
    from backend.config import get_settings as real_get_settings
    from backend import main as main_mod

    base = real_get_settings()
    for key, value in overrides.items():
        setattr(base, key, value)
    monkeypatch.setattr(main_mod, "get_settings", lambda: base)
    return main_mod.create_app()


def test_cors_wildcard_forces_credentials_off(monkeypatch):
    """通配来源 + credentials 是浏览器会拒绝的无效组合，必须被强制关掉。"""
    app = _build_app(monkeypatch, cors_origins="*", cors_allow_credentials=True)
    kwargs = _cors_kwargs(app)

    assert kwargs is not None
    assert kwargs["allow_origins"] == ["*"]
    assert kwargs["allow_credentials"] is False, "通配来源时不能开 credentials"


def test_cors_explicit_whitelist_allows_credentials(monkeypatch):
    """明确白名单下才允许携带凭证（Cookie 场景）。"""
    app = _build_app(monkeypatch, cors_origins="https://erp.example.com,http://localhost:3000",
                     cors_allow_credentials=True)
    kwargs = _cors_kwargs(app)

    assert kwargs["allow_origins"] == ["https://erp.example.com", "http://localhost:3000"]
    assert kwargs["allow_credentials"] is True


def test_cors_empty_disables_middleware(monkeypatch):
    """留空 = 不启用 CORS（同源部署：Vite 代理 / Nginx 反代）。"""
    app = _build_app(monkeypatch, cors_origins="")
    assert _cors_kwargs(app) is None


# ── ② 登录限流 ────────────────────────────────────────────────

def test_lockout_after_max_attempts(monkeypatch):
    from backend.config import get_settings
    s = get_settings()
    monkeypatch.setattr(s, "login_max_attempts", 3, raising=False)
    monkeypatch.setattr(s, "login_lockout_seconds", 60, raising=False)

    guard = LoginGuard()
    assert guard.locked_seconds("u@1.2.3.4") == 0
    assert guard.record_failure("u@1.2.3.4") == 0     # 1
    assert guard.record_failure("u@1.2.3.4") == 0     # 2
    locked = guard.record_failure("u@1.2.3.4")        # 3 → 达标
    assert locked > 0
    assert guard.locked_seconds("u@1.2.3.4") > 0


def test_lockout_is_per_key(monkeypatch):
    """锁的是"这个账号 + 这个来源"，不能把别人一起锁掉。"""
    from backend.config import get_settings
    s = get_settings()
    monkeypatch.setattr(s, "login_max_attempts", 2, raising=False)
    monkeypatch.setattr(s, "login_lockout_seconds", 60, raising=False)

    guard = LoginGuard()
    guard.record_failure("u@1.1.1.1")
    guard.record_failure("u@1.1.1.1")
    assert guard.locked_seconds("u@1.1.1.1") > 0
    assert guard.locked_seconds("u@2.2.2.2") == 0
    assert guard.locked_seconds("other@1.1.1.1") == 0


def test_successful_login_resets_counter(monkeypatch):
    from backend.config import get_settings
    s = get_settings()
    monkeypatch.setattr(s, "login_max_attempts", 3, raising=False)
    monkeypatch.setattr(s, "login_lockout_seconds", 60, raising=False)

    guard = LoginGuard()
    guard.record_failure("u@1.2.3.4")
    guard.record_failure("u@1.2.3.4")
    guard.reset("u@1.2.3.4")

    # 清零后重新计数：再失败 2 次也不该被锁
    assert guard.record_failure("u@1.2.3.4") == 0
    assert guard.record_failure("u@1.2.3.4") == 0


def test_lockout_expires(monkeypatch):
    """锁定到期后自动解锁，且失败计数一并清零（不然解锁后一次失败就又锁）。"""
    from backend.config import get_settings
    s = get_settings()
    monkeypatch.setattr(s, "login_max_attempts", 3, raising=False)
    monkeypatch.setattr(s, "login_lockout_seconds", 60, raising=False)

    guard = LoginGuard()
    for _ in range(3):
        guard.record_failure("u@1.2.3.4")
    assert guard.locked_seconds("u@1.2.3.4") > 0

    # 把解锁时间拨到过去，模拟窗口结束（不真等 60 秒）
    guard._locked_until["u@1.2.3.4"] = time.monotonic() - 1
    assert guard.locked_seconds("u@1.2.3.4") == 0
    assert guard.record_failure("u@1.2.3.4") == 0, "解锁后计数应已清零"


# ── ③ 探针分工 ────────────────────────────────────────────────

async def test_readiness_ok_when_all_healthy(monkeypatch):
    from backend.core import health

    async def _ok():
        return {"ok": True, "required": True}

    monkeypatch.setattr(health, "_PROBES", {"postgres": _ok, "milvus": _ok})
    payload, ready = await health.readiness()

    assert ready is True
    assert payload["status"] == "ok"
    assert "latency_ms" in payload["dependencies"]["postgres"]


async def test_readiness_degraded_when_optional_down(monkeypatch):
    """Redis 是可缺失依赖：它挂了只算 degraded，仍然 ready（fail-open 设计）。"""
    from backend.core import health

    async def _ok():
        return {"ok": True, "required": True}

    async def _redis_down():
        return {"ok": False, "required": False, "note": "fail-open"}

    monkeypatch.setattr(health, "_PROBES", {"postgres": _ok, "redis": _redis_down})
    payload, ready = await health.readiness()

    assert ready is True
    assert payload["status"] == "degraded"
    assert payload["dependencies"]["redis"]["ok"] is False


async def test_readiness_unavailable_when_required_down(monkeypatch):
    """必需依赖挂了就不就绪（编排层据此摘流量）。"""
    from backend.core import health

    async def _ok():
        return {"ok": True, "required": True}

    async def _pg_down():
        raise RuntimeError("connection refused")

    monkeypatch.setattr(health, "_PROBES", {"postgres": _pg_down, "milvus": _ok})
    payload, ready = await health.readiness()

    assert ready is False
    assert payload["status"] == "unavailable"
    assert "connection refused" in payload["dependencies"]["postgres"]["error"]


async def test_probe_timeout_is_reported(monkeypatch):
    """卡住的依赖不能把就绪探针拖死，要报超时。"""
    import asyncio
    from backend.core import health

    async def _hang():
        await asyncio.sleep(5)
        return {"ok": True, "required": True}

    monkeypatch.setattr(health, "_PROBE_TIMEOUT", 0.01)
    name, result = await health._run_probe("postgres", _hang)

    assert name == "postgres"
    assert result["ok"] is False
    assert "超时" in result["error"]


# ── ④ 演示账号默认关闭 ────────────────────────────────────────

def test_seed_demo_users_defaults_to_false():
    """发布默认值必须是 False：开源项目不能一启动就带人人皆知的口令。"""
    assert Settings.model_fields["seed_demo_users"].default is False


def test_cors_defaults_are_documented_shape():
    """默认通配来源 + 不带凭证（Token 鉴权，不需要 Cookie）。"""
    assert Settings.model_fields["cors_origins"].default == "*"
    assert Settings.model_fields["cors_allow_credentials"].default is False


def test_login_guard_defaults_are_sane():
    assert Settings.model_fields["login_max_attempts"].default >= 3
    assert Settings.model_fields["login_lockout_seconds"].default >= 60


# ── ⑤ 建号脚本可用（不播种演示账号时的替代路径）────────────────

def test_create_user_script_compiles_and_exposes_roles():
    import ast
    from pathlib import Path

    src = Path("scripts/create_user.py").read_text(encoding="utf-8")
    ast.parse(src)                      # 语法正确
    assert "worker" in src and "supervisor" in src and "admin" in src
    assert "getpass" in src, "密码应支持交互输入，避免进 shell 历史"
    assert "--reset" in src


def test_create_user_rejects_short_password():
    """弱密码在脚本层就挡掉（bcrypt 还有 72 字节上限）。"""
    import ast
    from pathlib import Path

    src = Path("scripts/create_user.py").read_text(encoding="utf-8")
    assert "len(password) < 8" in src
    assert "72" in src
    ast.parse(src)
