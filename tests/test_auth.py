# tests/test_auth.py
# 认证测试：bcrypt 哈希 / JWT 签发解码 / 登录接口

import pytest

from backend.dependencies import pwd_context, create_access_token
from tests.conftest import make_id


# ── 纯单元：密码哈希 ──────────────────────────────────────────

def test_password_hash_roundtrip():
    h = pwd_context.hash("Secret@123")
    assert h != "Secret@123"
    assert pwd_context.verify("Secret@123", h)
    assert not pwd_context.verify("Wrong@123", h)


def test_default_users_hashes_are_bcrypt():
    from backend.db.repositories import DEFAULT_USERS
    for u in DEFAULT_USERS:
        assert u["password_hash"].startswith("$2b$")
        assert u["username"] in ("caregiver", "supervisor", "admin")


# ── 纯单元：JWT ──────────────────────────────────────────────

def test_jwt_create_and_decode():
    from jose import jwt as jose_jwt
    from backend.config import get_settings
    settings = get_settings()

    token = create_access_token({"sub": "u-admin", "role": "admin", "tenant_id": "t1"})
    payload = jose_jwt.decode(token, settings.jwt_secret_key,
                              algorithms=[settings.jwt_algorithm])
    assert payload["sub"] == "u-admin"
    assert payload["role"] == "admin"
    assert "exp" in payload


def test_jwt_wrong_signature_rejected():
    from jose import jwt as jose_jwt, JWTError
    from backend.config import get_settings
    settings = get_settings()

    token = create_access_token({"sub": "u-admin"})
    with pytest.raises(JWTError):
        jose_jwt.decode(token, "wrong-secret", algorithms=[settings.jwt_algorithm])


# ── 集成：DB 验证 + 登录接口（依赖 PG）────────────────────────

@pytest.mark.asyncio
async def test_verify_user_against_db(db_ready):
    from backend.dependencies import verify_user

    user = await verify_user("admin", "Admin@123")
    assert user is not None
    assert user["role"] == "admin"
    assert user["tenant_id"] == "tenant_default"

    # 密码错误 / 用户不存在
    assert await verify_user("admin", "wrong-password") is None
    assert await verify_user(make_id("nouser"), "whatever") is None


@pytest.mark.asyncio
async def test_login_api(db_ready):
    import httpx
    from backend.main import app

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        # 正确账号
        resp = await client.post("/api/v1/auth/login", json={
            "username": "supervisor", "password": "Supervisor@123"})
        assert resp.status_code == 200
        body = resp.json()
        assert body["token_type"] == "bearer"
        assert body["role"] == "supervisor"
        assert body["access_token"]

        # 密码错误 → 401
        resp = await client.post("/api/v1/auth/login", json={
            "username": "supervisor", "password": "bad"})
        assert resp.status_code == 401
