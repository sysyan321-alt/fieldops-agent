#!/usr/bin/env python3
# scripts/check_fresh_db.py
# 全新环境冒烟检查：空库 → 建表脚本 → 启动迁移，验证"第一次部署"这条路是通的。
#
# 为什么需要：开发者本机的 postgres 数据卷早就初始化过了，`scripts/init_db.sql`
# 与启动迁移（`backend/db/migrations.py`）一旦漂移，只会在"新克隆仓库的人"那里炸。
# 真实踩过的坑：建表脚本末尾给一张只存在于启动迁移里的表建索引，而 postgres 官方入口
# 脚本是用 ON_ERROR_STOP=1 执行 init 脚本的 → 报 "relation does not exist" → 容器 exit=3，
# 数据库根本起不来。这类问题在本地永远看不到，只能靠在干净库上真跑一遍暴露。
#
# 检查三件事：
#   ① 建表脚本能在空库上一条不报地跑完（DDL 报错、顺序写反都会当场失败）
#   ② 建出来的表数量与预期一致
#   ③ 启动迁移零失败（skipped == 0）——证明两张 DDL 来源没有漂移
#
# 用法：python scripts/check_fresh_db.py       （需要 postgres 容器已启动）
#   默认走 `docker compose exec postgres`（CI 与本机 compose 环境都是这个）；
#   本机若共用别的 compose 项目（例如 care_agent 那套容器），指定容器名即可：
#     PG_CONTAINER=care_agent_postgres python scripts/check_fresh_db.py
# 退出码：0=通过；1=失败（打印失败的那条语句）

import asyncio
import os
import pathlib
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
SCRATCH_DB = "fieldops_initcheck"
EXPECTED_TABLES = 17


def load_env() -> None:
    """读 .env.local 拿连接参数（不覆盖已存在的环境变量）。"""
    env_path = ROOT / ".env.local"
    if not env_path.exists():
        sys.exit("✗ 找不到 .env.local：先 cp .env.example .env.local 再跑")
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip())


def psql(sql: str, db: str = "postgres", quiet: bool = True,
         scalar: bool = False) -> subprocess.CompletedProcess:
    """在 postgres 容器里执行 SQL。

    ON_ERROR_STOP=1：任何一条报错立刻停下并以非零码退出——这正是 docker 入口脚本
    执行 init_db.sql 时的行为，检查要跟它对齐。
    """
    if os.environ.get("PG_CONTAINER"):        # 本机复用别的 compose 项目时用
        cmd = ["docker", "exec", "-i", os.environ["PG_CONTAINER"]]
    else:
        cmd = ["docker", "compose", "exec", "-T", "postgres"]
    cmd += ["psql", "-U", os.environ["DB_USER"], "-d", db, "-v", "ON_ERROR_STOP=1"]
    if scalar:                                # 只取值：不打印表头、不对齐
        cmd.append("-tA")
    elif quiet:
        cmd.append("-q")
    return subprocess.run(cmd, input=sql, text=True, cwd=ROOT, capture_output=True)


def fail(step: str, result: subprocess.CompletedProcess) -> None:
    print(f"✗ {step} 失败")
    print((result.stderr or result.stdout).strip()[-1500:])
    sys.exit(1)


def main() -> None:
    load_env()
    # 迁移也指向临时库：整个检查不会碰开发库
    os.environ["DB_NAME"] = SCRATCH_DB

    print(f"[1/4] 创建临时库 {SCRATCH_DB}")
    if psql(f"DROP DATABASE IF EXISTS {SCRATCH_DB};").returncode != 0:
        fail("删除同名临时库", psql(f"DROP DATABASE IF EXISTS {SCRATCH_DB};"))
    r = psql(f"CREATE DATABASE {SCRATCH_DB} OWNER {os.environ['DB_USER']};")
    if r.returncode != 0:
        fail("创建临时库", r)

    try:
        print("[2/4] 空库执行 scripts/init_db.sql")
        r = psql((ROOT / "scripts" / "init_db.sql").read_text(encoding="utf-8"),
                 db=SCRATCH_DB, quiet=False)
        if r.returncode != 0:
            fail("建表脚本执行", r)

        r = psql("SELECT count(*) FROM information_schema.tables WHERE table_schema='public';",
                 db=SCRATCH_DB, scalar=True)
        tables = int(r.stdout.strip())
        print(f"      → 建表完成：{tables} 张表")
        if tables != EXPECTED_TABLES:
            sys.exit(f"✗ 表数量不符：期望 {EXPECTED_TABLES}，实际 {tables}"
                     "（改了表结构就把 EXPECTED_TABLES 一起改）")

        print("[3/4] 执行启动迁移（必须零失败）")
        sys.path.insert(0, str(ROOT))
        from backend.db.migrations import run_migrations  # noqa: E402

        applied, skipped = asyncio.run(run_migrations())
        print(f"      → applied={applied} skipped={skipped}")
        if skipped:
            sys.exit(f"✗ 有 {skipped} 条迁移语句失败（看上面的 migrations.statement_failed 日志）："
                     "建表脚本与启动迁移已经漂移，或者迁移顺序写反了")
        if not applied:
            sys.exit("✗ 一条迁移都没执行，检查 _MIGRATIONS 是否为空")

        # 必须先把连接池关掉：asyncpg 的连接还挂着，DROP DATABASE 会报
        # "database is being accessed by other users"，临时库就漏在服务器上了
        async def _dispose() -> None:
            from backend.dependencies import engine
            await engine.dispose()

        asyncio.run(_dispose())
    finally:
        print(f"[4/4] 删除临时库 {SCRATCH_DB}")
        # WITH (FORCE)：万一还有别的连接挂着，也直接踢掉——这是本脚本自己建的临时库
        r = psql(f"DROP DATABASE IF EXISTS {SCRATCH_DB} WITH (FORCE);")
        if r.returncode != 0:
            print(f"⚠️ 临时库 {SCRATCH_DB} 没删掉，请手工清理：\n"
                  f"{(r.stderr or r.stdout).strip()[-400:]}")

    print("✅ 全新环境初始化路径通过：建表脚本 → 启动迁移，零报错")


if __name__ == "__main__":
    main()
