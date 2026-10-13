#!/usr/bin/env python3
# scripts/create_user.py
# 创建/更新登录账号（默认不播种演示账号，所以需要它来建第一个管理员）。
#
# 用法：
#   python scripts/create_user.py --username admin --role admin
#   python scripts/create_user.py --username nurse01 --role worker --tenant tenant_default
#   python scripts/create_user.py --username admin --password 'xxx' --reset
#
# 不传 --password 时会交互式输入（不回显），避免密码进 shell 历史。

import argparse
import asyncio
import getpass
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

VALID_ROLES = ("worker", "supervisor", "admin")


async def _run(args) -> int:
    from sqlalchemy import text

    from backend.dependencies import AsyncSessionLocal, pwd_context

    password = args.password
    if not password:
        password = getpass.getpass(f"请输入 {args.username} 的密码: ")
        confirm = getpass.getpass("再输一次确认: ")
        if password != confirm:
            print("❌ 两次输入不一致")
            return 1
    if len(password) < 8:
        print("❌ 密码至少 8 位（bcrypt 也要求不超过 72 字节）")
        return 1
    if len(password.encode("utf-8")) > 72:
        print("❌ 密码超过 72 字节，bcrypt 会报错，请缩短")
        return 1

    password_hash = pwd_context.hash(password)
    async with AsyncSessionLocal() as session:
        existing = (await session.execute(text(
            "SELECT user_id FROM users WHERE username = :u"), {"u": args.username})).first()

        if existing and not args.reset:
            print(f"❌ 用户已存在: {args.username}（要改密码请加 --reset）")
            return 1

        if existing:
            await session.execute(text(
                "UPDATE users SET password_hash = :h, role = :r, tenant_id = :t"
                " WHERE username = :u"),
                {"h": password_hash, "r": args.role, "t": args.tenant, "u": args.username})
            action = "已重置密码"
        else:
            await session.execute(text("""
                INSERT INTO users (user_id, username, password_hash, role, tenant_id)
                VALUES (:uid, :u, :h, :r, :t)
                ON CONFLICT (username) DO UPDATE SET
                    password_hash = EXCLUDED.password_hash, role = EXCLUDED.role
            """), {"uid": f"u-{uuid.uuid4().hex[:12]}", "u": args.username,
                   "h": password_hash, "r": args.role, "t": args.tenant})
            action = "已创建"
        await session.commit()

    print(f"✅ {action}: {args.username}（角色 {args.role}，租户 {args.tenant}）")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="创建或重置 FieldOps 登录账号")
    parser.add_argument("--username", required=True)
    parser.add_argument("--password", default="", help="省略则交互式输入（推荐）")
    parser.add_argument("--role", default="admin", choices=VALID_ROLES)
    parser.add_argument("--tenant", default="tenant_default")
    parser.add_argument("--reset", action="store_true", help="用户已存在时重置密码与角色")
    args = parser.parse_args()
    return asyncio.run(_run(args))


if __name__ == "__main__":
    sys.exit(main())
