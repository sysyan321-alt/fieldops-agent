# backend/core/login_guard.py
# 登录失败限流：同一账号在时间窗内连续失败达阈值后临时锁定。
#
# 为什么需要：本项目启动即带演示账号（可选），且部署形态常见于服务组织内网。
# 没有限流时，一个脚本就能把 worker/Worker@123 这类弱口令撞开。
#
# 实现取舍：进程内计数，不引入 Redis 依赖——缓存层是 fail-open 的，
# 把安全措施挂在可失败的外部依赖上不划算。代价是多 worker 部署时计数不共享
# （每个 worker 各自计数），对"防暴力破解"这个目标够用；要严格共享需换成 Redis 计数器。

import time
from collections import defaultdict

from backend.config import get_settings
from backend.core.logger import get_logger

logger = get_logger(__name__)


class LoginGuard:
    """按 key（通常是 用户名 + 客户端 IP）统计失败次数并临时锁定。"""

    def __init__(self) -> None:
        # key -> [失败时间戳...]；只保留锁定窗口内的记录
        self._failures: dict[str, list[float]] = defaultdict(list)
        self._locked_until: dict[str, float] = {}

    # ── 内部小工具 ──

    def _cfg(self) -> tuple[int, int]:
        s = get_settings()
        return max(int(s.login_max_attempts), 1), max(int(s.login_lockout_seconds), 1)

    @staticmethod
    def _now() -> float:
        return time.monotonic()

    # ── 对外接口 ──

    def locked_seconds(self, key: str) -> int:
        """还剩多少秒解锁；未锁定返回 0。"""
        until = self._locked_until.get(key)
        if not until:
            return 0
        remaining = until - self._now()
        if remaining <= 0:
            self._locked_until.pop(key, None)
            self._failures.pop(key, None)
            return 0
        return int(remaining) + 1

    def record_failure(self, key: str) -> int:
        """记一次失败；达到阈值则锁定，返回剩余锁定秒数（未锁定返回 0）。"""
        max_attempts, lockout = self._cfg()
        now = self._now()
        # 只保留锁定窗口内的失败，避免"隔几天失败几次"被累计
        recent = [t for t in self._failures.get(key, []) if now - t < lockout]
        recent.append(now)
        self._failures[key] = recent
        if len(recent) >= max_attempts:
            self._locked_until[key] = now + lockout
            logger.warning("login.locked", key=key, attempts=len(recent),
                           lockout_seconds=lockout)
            return self.locked_seconds(key)
        logger.info("login.failed", key=key, attempts=len(recent), max_attempts=max_attempts)
        return 0

    def reset(self, key: str) -> None:
        """登录成功后清零该 key 的失败记录。"""
        self._failures.pop(key, None)
        self._locked_until.pop(key, None)


_guard: LoginGuard | None = None


def get_login_guard() -> LoginGuard:
    """获取全局登录守卫（进程内单例）。"""
    global _guard
    if _guard is None:
        _guard = LoginGuard()
    return _guard


def reset_login_guard() -> None:
    """清空全部计数（测试用）。"""
    global _guard
    _guard = LoginGuard()
