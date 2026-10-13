# backend/dependencies.py
# FastAPI 依赖注入：数据库会话 + JWT 认证

from typing import AsyncGenerator, Optional
from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
from sqlalchemy.ext.asyncio import (
    AsyncSession, async_sessionmaker, create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase
from jose import jwt, JWTError

from backend.config import get_settings
from backend.core.logger import get_logger

logger = get_logger(__name__)

# ── 数据库引擎与 Session 工厂 ─────────────────────────────────

settings = get_settings()

engine = create_async_engine(
    settings.database_url,
    echo=settings.app_debug,
    pool_size=10,
    max_overflow=20,
    pool_pre_ping=True,
)

AsyncSessionLocal = async_sessionmaker(
    engine, class_=AsyncSession, expire_on_commit=False,
)


class Base(DeclarativeBase):
    """SQLAlchemy ORM 基类。"""
    pass


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """FastAPI 依赖：获取数据库会话（用完自动关闭）。"""
    async with AsyncSessionLocal() as session:
        try:
            yield session
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()


# ── JWT 认证 ───────────────────────────────────────────────────

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/v1/auth/login")

# 密码哈希上下文（bcrypt）
# 兼容性补丁：passlib 1.7.4 要读 bcrypt.__about__.__version__，而 bcrypt>=4 删了它
import bcrypt as _bcrypt_mod, types as _types
if not hasattr(_bcrypt_mod, "__about__"):
    _bcrypt_mod.__about__ = _types.SimpleNamespace(
        __version__=getattr(_bcrypt_mod, "__version__", "4.x")
    )
from passlib.context import CryptContext

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")


async def verify_user(username: str, password: str) -> Optional[dict]:
    """验证用户密码（数据库查询 + bcrypt 比对）。"""
    from backend.db.repositories import get_user_by_username

    user = await get_user_by_username(username)
    if user and pwd_context.verify(password, user["password_hash"]):
        return {
            "username": user["username"],
            "role": user["role"],
            "user_id": user["user_id"],
            "tenant_id": user["tenant_id"],
        }
    return None


def create_access_token(data: dict) -> str:
    """签发 JWT 令牌。"""
    import datetime
    to_encode = data.copy()
    expire = datetime.datetime.utcnow() + datetime.timedelta(
        minutes=settings.jwt_access_token_expire_minutes
    )
    to_encode.update({"exp": expire})
    return jwt.encode(
        to_encode, settings.jwt_secret_key, algorithm=settings.jwt_algorithm,
    )


async def get_current_user(token: str = Depends(oauth2_scheme)) -> dict:
    """FastAPI 依赖：从 JWT 解析当前用户。"""
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="无法验证凭证",
        headers={"WWW-Authenticate": "Bearer"},
    )
    try:
        payload = jwt.decode(
            token, settings.jwt_secret_key,
            algorithms=[settings.jwt_algorithm],
        )
        user_id: str = payload.get("sub", "")
        role: str = payload.get("role", "")
        tenant_id: str = payload.get("tenant_id", "")
        if not user_id:
            raise credentials_exception
        return {"user_id": user_id, "role": role, "tenant_id": tenant_id}
    except JWTError:
        raise credentials_exception
