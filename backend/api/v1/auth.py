# backend/api/v1/auth.py
# 认证路由：登录返回 JWT

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel

from backend.core.login_guard import get_login_guard
from backend.core.logger import get_logger
from backend.dependencies import verify_user, create_access_token

router = APIRouter(prefix="/auth")
logger = get_logger(__name__)


class LoginRequest(BaseModel):
    """登录请求。"""
    username: str
    password: str


class LoginResponse(BaseModel):
    """登录响应。"""
    access_token: str
    token_type:   str = "bearer"
    role:         str
    user_id:      str


@router.post("/login", response_model=LoginResponse)
async def login(req: LoginRequest, request: Request):
    """登录接口：数据库验证 + bcrypt 密码比对，返回 JWT 令牌。

    带失败限流：同一（用户名 + 来源 IP）在窗口内连续失败达阈值后临时锁定，
    避免默认/弱口令被脚本撞开。客户端 IP 取 X-Forwarded-For 首个地址（反代场景），
    没有则用直连地址。
    """
    forwarded = (request.headers.get("x-forwarded-for") or "").split(",")[0].strip()
    client_host = forwarded or (request.client.host if request.client else "unknown")
    guard_key = f"{req.username}@{client_host}"

    guard = get_login_guard()
    locked = guard.locked_seconds(guard_key)
    if locked:
        logger.warning("auth.login_locked", username=req.username, host=client_host,
                       retry_after=locked)
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=f"登录失败次数过多，请 {locked} 秒后再试",
            headers={"Retry-After": str(locked)},
        )

    user = await verify_user(req.username, req.password)
    if not user:
        still_locked = guard.record_failure(guard_key)
        if still_locked:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail=f"登录失败次数过多，请 {still_locked} 秒后再试",
                headers={"Retry-After": str(still_locked)},
            )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="用户名或密码错误",
        )

    guard.reset(guard_key)
    token = create_access_token({
        "sub": user["user_id"],
        "role": user["role"],
        "tenant_id": user["tenant_id"],
    })
    return LoginResponse(
        access_token=token,
        role=user["role"],
        user_id=user["user_id"],
    )
