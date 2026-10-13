#!/usr/bin/env python3
# scripts/seed_data.py
# FieldOps 种子数据：5 位老人、10 台设备、3 份照护计划、20 条照护记录

import asyncio
import os
import sys
from datetime import datetime, timedelta
import json

# 把项目根目录加入 sys.path
fieldops_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, fieldops_root)


async def seed_data():
    """插入种子数据（使用 asyncpg 直连 PostgreSQL）。"""
    import asyncpg

    # 从环境变量读取数据库连接信息
    DB_HOST = os.getenv("DB_HOST", "localhost")
    DB_PORT = int(os.getenv("DB_PORT", "5433"))
    DB_NAME = os.getenv("DB_NAME", "fieldops")
    DB_USER = os.getenv("DB_USER", "postgres")
    DB_PASSWORD = os.getenv("DB_PASSWORD", "your_password")

    print(f"连接数据库: {DB_HOST}:{DB_PORT}/{DB_NAME}")

    conn = await asyncpg.connect(
        host=DB_HOST, port=DB_PORT,
        database=DB_NAME, user=DB_USER, password=DB_PASSWORD,
    )

    try:
        # created_at 列为 naive TIMESTAMP，与 Agent 节点保持一致使用本地 naive 时间
        now = datetime.now()

        # ──────────────────────────────────────────────────
        # 1. 插入 5 位老人
        # ──────────────────────────────────────────────────
        sites = [
            ("e001", "张明德", 78, "男", "101", "二级"),
            ("e002", "李桂芳", 82, "女", "102", "一级"),
            ("e003", "王建国", 75, "男", "103", "三级"),
            ("e004", "赵秀英", 80, "女", "104", "二级"),
            ("e005", "刘福生", 85, "男", "201", "一级"),
        ]
        for site in sites:
            await conn.execute(
                """INSERT INTO sites (site_id, name, built_year, site_type, location, risk_tier, tenant_id)
                   VALUES ($1, $2, $3, $4, $5, $6, 'tenant_default')
                   ON CONFLICT (site_id) DO NOTHING""",
                *site,
            )
        print(f"插入老人: {len(sites)} 条")

        # ──────────────────────────────────────────────────
        # 2. 插入 10 台设备
        # ──────────────────────────────────────────────────
        devices = [
            ("dev-001", "e001", "心率手环",     "active"),
            ("dev-002", "e001", "离床传感器",   "active"),
            ("dev-003", "e002", "心率手环",     "active"),
            ("dev-004", "e002", "跌倒检测",     "active"),
            ("dev-005", "e003", "温湿度传感器", "active"),
            ("dev-006", "e003", "离床传感器",   "active"),
            ("dev-007", "e004", "心率手环",     "active"),
            ("dev-008", "e004", "血氧仪",       "active"),
            ("dev-009", "e005", "跌倒检测",     "active"),
            ("dev-010", "e005", "离床传感器",   "maintenance"),
        ]
        for dev in devices:
            await conn.execute(
                """INSERT INTO devices (device_id, site_id, device_type, status)
                   VALUES ($1, $2, $3, $4)
                   ON CONFLICT (device_id) DO NOTHING""",
                *dev,
            )
        print(f"插入设备: {len(devices)} 条")

        # ──────────────────────────────────────────────────
        # 3. 插入 3 份照护计划
        # ──────────────────────────────────────────────────
        service_plans = [
            ("plan-001", "e001", {
                "risk_tier": "二级",
                "daily_tasks": ["晨间护理", "三餐协助", "每2小时翻身", "按时发药"],
                "special_notes": "高血压，需监测血压",
            }),
            ("plan-002", "e002", {
                "risk_tier": "一级",
                "daily_tasks": ["全天候照护", "鼻饲管护理", "每1小时翻身", "压疮预防"],
                "special_notes": "卧床老人，需重点防压疮",
            }),
            ("plan-003", "e004", {
                "risk_tier": "二级",
                "daily_tasks": ["晨间护理", "协助进食", "每2小时翻身", "用药管理"],
                "special_notes": "轻度认知障碍，需反复提醒",
            }),
        ]
        for plan in service_plans:
            await conn.execute(
                """INSERT INTO service_plans (plan_id, site_id, plan_content)
                   VALUES ($1, $2, $3)
                   ON CONFLICT (plan_id) DO NOTHING""",
                plan[0], plan[1], json.dumps(plan[2], ensure_ascii=False),
            )
        print(f"插入照护计划: {len(service_plans)} 条")

        # ──────────────────────────────────────────────────
        # 4. 插入 20 条照护记录
        # ──────────────────────────────────────────────────
        service_types = ["巡视", "翻身", "喂药", "进食", "如厕", "口腔护理", "皮肤检查", "晚间护理"]
        contents = {
            "巡视": "老人安静休息，无异常",
            "翻身": "协助翻身，皮肤完好",
            "喂药": "按时服用降压药",
            "进食": "进食量正常",
            "如厕": "协助如厕，无异常",
            "口腔护理": "完成口腔清洁",
            "皮肤检查": "皮肤完整，无压疮",
            "晚间护理": "协助就寝，检查安全",
        }
        site_ids = ["e001", "e002", "e003", "e004", "e005"]
        worker_ids = ["cg-001", "cg-002", "cg-003"]

        for i in range(20):
            site_id = site_ids[i % len(site_ids)]
            ct = service_types[i % len(service_types)]
            cg = worker_ids[i % len(worker_ids)]
            ts = now - timedelta(hours=i * 2)
            await conn.execute(
                """INSERT INTO service_records (record_id, site_id, service_type, content, worker_id, created_at)
                   VALUES ($1, $2, $3, $4, $5, $6)""",
                f"rec-seed-{i+1:03d}", site_id, ct, contents[ct], cg, ts,
            )
        print(f"插入照护记录: 20 条")

        print("\n✅ 种子数据插入完成！")

    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(seed_data())
