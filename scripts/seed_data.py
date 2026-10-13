#!/usr/bin/env python3
# scripts/seed_data.py
# FieldOps 种子数据：5 位点位、10 台设备、3 份服务计划、20 条服务记录

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
        # 1. 插入 5 个点位（演示数据，均为合成内容）
        #    字段顺序：site_id, name, built_year, site_type, location, risk_tier
        # ──────────────────────────────────────────────────
        sites = [
            ("s001", "3号楼B1配电间",   2008, "配电间",   "3号楼B1",     "一级"),
            ("s002", "1号楼屋面",       2012, "公共区域", "1号楼顶",     "二级"),
            ("s003", "地下车库集水井",   2015, "设备间",   "B2车库东侧",  "一级"),
            ("s004", "食堂操作间",       2010, "公共区域", "1层东侧",     "二级"),
            ("s005", "消防水泵房",       2008, "设备间",   "B1西侧",      "三级"),
        ]
        for site in sites:
            await conn.execute(
                """INSERT INTO sites (site_id, name, built_year, site_type, location, risk_tier, tenant_id)
                   VALUES ($1, $2, $3, $4, $5, $6, 'tenant_default')
                   ON CONFLICT (site_id) DO NOTHING""",
                *site,
            )
        print(f"插入点位: {len(sites)} 条")

        # ──────────────────────────────────────────────────
        # 2. 插入 10 台现场设备（类型对齐 device_types 登记表）
        # ──────────────────────────────────────────────────
        devices = [
            ("dev-001", "s001", "配电监测",     "active"),
            ("dev-002", "s001", "环境监测站",   "active"),
            ("dev-003", "s002", "视频行为分析", "active"),
            ("dev-004", "s002", "水位监测",     "active"),
            ("dev-005", "s003", "水位监测",     "active"),
            ("dev-006", "s003", "配电监测",     "active"),
            ("dev-007", "s004", "环境监测站",   "active"),
            ("dev-008", "s004", "视频行为分析", "active"),
            ("dev-009", "s005", "配电监测",     "active"),
            ("dev-010", "s005", "门禁",         "maintenance"),
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
        # 3. 插入 3 份服务计划
        # ──────────────────────────────────────────────────
        service_plans = [
            ("plan-001", "s001", {
                "risk_tier": "一级",
                "daily_tasks": ["日常巡检（6次/日）", "设备状态检查（3次/日）",
                                "用火用电检查（2次/日）"],
                "special_notes": "建于 2008 年，柜内接点有温升记录，需重点监测",
            }),
            ("plan-002", "s002", {
                "risk_tier": "二级",
                "daily_tasks": ["日常巡检（3次/日）", "消防设施外观检查（1次/日）"],
                "special_notes": "屋面防水层已到评估年限，雨季前需复查",
            }),
            ("plan-003", "s004", {
                "risk_tier": "二级",
                "daily_tasks": ["日常巡检（3次/日）", "环境检查（1次/日）",
                                "用火用电检查（2次/日）"],
                "special_notes": "明火作业区域，可燃物需每班清理",
            }),
        ]
        for plan in service_plans:
            await conn.execute(
                """INSERT INTO service_plans (plan_id, site_id, plan_content)
                   VALUES ($1, $2, $3)
                   ON CONFLICT (plan_id) DO NOTHING""",
                plan[0], plan[1], json.dumps(plan[2], ensure_ascii=False),
            )
        print(f"插入服务计划: {len(service_plans)} 条")

        # ──────────────────────────────────────────────────
        # 4. 插入 20 条服务记录（演示数据）
        # ──────────────────────────────────────────────────
        service_types = ["巡检", "设备检查", "环境检查", "消防设施检查",
                         "用电检查", "清洁整理", "润滑保养", "标识检查"]
        contents = {
            "巡检": "按路线完成巡检，无异常",
            "设备检查": "设备运行正常，无异常声响",
            "环境检查": "温湿度正常，无可燃物堆放",
            "消防设施检查": "灭火器压力在绿区，栓门开启正常",
            "用电检查": "无私拉乱接，配电箱门关闭到位",
            "清洁整理": "清理地面与柜前通道，无杂物",
            "润滑保养": "按周期加注润滑脂，油位正常",
            "标识检查": "安全标识清晰，无遮挡",
        }
        site_ids = ["s001", "s002", "s003", "s004", "s005"]
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
        print(f"插入服务记录: 20 条")

        print("\n✅ 种子数据插入完成！")

    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(seed_data())
