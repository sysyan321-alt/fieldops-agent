# backend/db/migrations.py
# 数据库迁移：应用启动时执行的幂等 DDL 补丁（替代占位实现）。
#
# 与 scripts/init_db.sql 的关系：
#   - init_db.sql 负责全新环境的完整建表（容器首次启动自动执行）
#   - 本模块负责已有环境的增量补丁（ALTER / CREATE IF NOT EXISTS，可重复执行）
# 两者保持同步：init_db.sql 里新增的表/列，这里也要有对应的幂等语句。

from sqlalchemy import text

from backend.dependencies import AsyncSessionLocal
from backend.core.logger import get_logger

logger = get_logger(__name__)

# 幂等 DDL 语句列表（按顺序执行）
_MIGRATIONS: list[str] = [
    # v1：告警表增加风险评估与四轨数据（供 GET /alert/{id}/review 读取）
    "ALTER TABLE alerts ADD COLUMN IF NOT EXISTS risk_assessment JSONB",
    "ALTER TABLE alerts ADD COLUMN IF NOT EXISTS three_tracks JSONB",
    # v1：照护流程报告表（save_report 节点落库）
    """
    CREATE TABLE IF NOT EXISTS work_flow_reports (
        report_id     VARCHAR(64) PRIMARY KEY,
        session_id    VARCHAR(64) NOT NULL UNIQUE,
        site_id      VARCHAR(64) NOT NULL,
        service_plan_id  VARCHAR(64) DEFAULT '',
        stage_reached VARCHAR(50),
        total_turns   INTEGER DEFAULT 0,
        report        JSONB,
        created_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (site_id) REFERENCES sites(site_id)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_work_flow_reports_site ON work_flow_reports(site_id)",
    # v2：用户表（登录认证，DB + bcrypt）
    """
    CREATE TABLE IF NOT EXISTS users (
        user_id       VARCHAR(64) PRIMARY KEY,
        username      VARCHAR(64) NOT NULL UNIQUE,
        password_hash VARCHAR(100) NOT NULL,
        role          VARCHAR(20) NOT NULL,
        tenant_id     VARCHAR(64) NOT NULL DEFAULT 'tenant_default',
        created_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_users_username ON users(username)",
    # v4：多租户强制隔离——业务表贯通 tenant_id（sites/users 建表时已有）
    "ALTER TABLE devices ADD COLUMN IF NOT EXISTS tenant_id VARCHAR(64) NOT NULL DEFAULT 'tenant_default'",
    "ALTER TABLE alerts ADD COLUMN IF NOT EXISTS tenant_id VARCHAR(64) NOT NULL DEFAULT 'tenant_default'",
    "ALTER TABLE service_plans ADD COLUMN IF NOT EXISTS tenant_id VARCHAR(64) NOT NULL DEFAULT 'tenant_default'",
    "ALTER TABLE service_records ADD COLUMN IF NOT EXISTS tenant_id VARCHAR(64) NOT NULL DEFAULT 'tenant_default'",
    "ALTER TABLE assessment_results ADD COLUMN IF NOT EXISTS tenant_id VARCHAR(64) NOT NULL DEFAULT 'tenant_default'",
    "ALTER TABLE work_flow_reports ADD COLUMN IF NOT EXISTS tenant_id VARCHAR(64) NOT NULL DEFAULT 'tenant_default'",
    "ALTER TABLE dispatch_orders ADD COLUMN IF NOT EXISTS tenant_id VARCHAR(64) NOT NULL DEFAULT 'tenant_default'",
    "ALTER TABLE device_readings ADD COLUMN IF NOT EXISTS tenant_id VARCHAR(64) NOT NULL DEFAULT 'tenant_default'",
    "CREATE INDEX IF NOT EXISTS idx_alerts_tenant ON alerts(tenant_id)",
    "CREATE INDEX IF NOT EXISTS idx_assessment_tenant ON assessment_results(tenant_id)",
    "CREATE INDEX IF NOT EXISTS idx_work_flow_tenant ON work_flow_reports(tenant_id)",
    "CREATE INDEX IF NOT EXISTS idx_sites_tenant ON sites(tenant_id)",
    "CREATE INDEX IF NOT EXISTS idx_dr_tenant ON device_readings(tenant_id)",
    # v5：出站通知（业务事件 → 外部 SaaS）——租户回调配置 + 事件发件箱
    """
    CREATE TABLE IF NOT EXISTS tenant_webhooks (
        tenant_id     VARCHAR(64) PRIMARY KEY,
        url           VARCHAR(512) NOT NULL,
        secret        VARCHAR(128) NOT NULL DEFAULT '',
        enabled       BOOLEAN NOT NULL DEFAULT TRUE,
        updated_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS outbound_events (
        event_id      VARCHAR(64) PRIMARY KEY,
        tenant_id     VARCHAR(64) NOT NULL DEFAULT 'tenant_default',
        event_type    VARCHAR(64) NOT NULL,
        aggregate_id  VARCHAR(64) NOT NULL,
        payload       JSONB NOT NULL,
        status        VARCHAR(20) NOT NULL DEFAULT 'pending',
        attempts      INTEGER NOT NULL DEFAULT 0,
        last_error    TEXT DEFAULT '',
        next_retry_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        created_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        sent_at       TIMESTAMP
    )
    """,
    # 幂等键：同一业务主体的同一类事件只落一条，重放安全（save_report 可能被重跑）
    "CREATE UNIQUE INDEX IF NOT EXISTS uniq_outbound_event_agg ON outbound_events(event_type, aggregate_id)",
    "CREATE INDEX IF NOT EXISTS idx_outbound_pending ON outbound_events(status, next_retry_at)",
    "CREATE INDEX IF NOT EXISTS idx_outbound_tenant ON outbound_events(tenant_id)",
    # v6：设备谱系对齐机构 SaaS
    #   device_types —— 设备类型字典（编码对齐业务系统的设备类型字典）
    #   devices      —— 补类型编码与安装位置（对齐 SaaS 的"设备编号/类型/状态/安装位置"）
    #   alerts       —— 补 source/external_alert_id/location/handled_by/handled_at
    #                   （对齐 SaaS 报警记录 报警记录 的"报警位置/状态/处理人/处理时间"）
    """
    CREATE TABLE IF NOT EXISTS device_types (
        type_code    VARCHAR(50) PRIMARY KEY,
        type_name    VARCHAR(64) NOT NULL,
        metrics      JSONB NOT NULL DEFAULT '[]',
        alert_types  JSONB NOT NULL DEFAULT '[]',
        trust_level  SMALLINT NOT NULL DEFAULT 3,
        trust_reason TEXT DEFAULT '',
        tenant_id    VARCHAR(64) NOT NULL DEFAULT 'tenant_default'
    )
    """,
    "ALTER TABLE devices ADD COLUMN IF NOT EXISTS type_code VARCHAR(50)",
    "ALTER TABLE devices ADD COLUMN IF NOT EXISTS location VARCHAR(128) DEFAULT ''",
    "CREATE INDEX IF NOT EXISTS idx_devices_type ON devices(type_code)",
    "ALTER TABLE alerts ADD COLUMN IF NOT EXISTS source VARCHAR(16) NOT NULL DEFAULT 'local'",
    "ALTER TABLE alerts ADD COLUMN IF NOT EXISTS external_alert_id VARCHAR(64)",
    "ALTER TABLE alerts ADD COLUMN IF NOT EXISTS location VARCHAR(128) DEFAULT ''",
    "ALTER TABLE alerts ADD COLUMN IF NOT EXISTS handled_by VARCHAR(64) DEFAULT ''",
    "ALTER TABLE alerts ADD COLUMN IF NOT EXISTS handled_at TIMESTAMP",
    # 同一租户下一条 SaaS 报警只生成一条 care 告警（下行重放安全）
    "CREATE UNIQUE INDEX IF NOT EXISTS uniq_alert_external ON alerts(tenant_id, external_alert_id) "
    "WHERE external_alert_id IS NOT NULL",
    # v7：照护计划对齐机构 SaaS 的业务模型
    #   site_service_items —— 老人的照护服务项（对齐业务系统的服务内容 /
    #                          addSiteServiceContent：项目 + 频次 + 护理时间段 + 必选）
    #   daily_work_plans    —— 每日照护计划（对齐业务系统：服务项目/老人/护理时间/
    #                          服务人员/实际护理时间/状态）。care 侧**只读**：
    #                          生成规则属于业务侧（每日计划由业务系统生成）
    """
    CREATE TABLE IF NOT EXISTS site_service_items (
        item_id         VARCHAR(64) PRIMARY KEY,
        site_id        VARCHAR(64) NOT NULL,
        project_id      VARCHAR(64) DEFAULT '',
        project_name    VARCHAR(128) NOT NULL,
        frequency_id    VARCHAR(64) DEFAULT '',
        frequency_name  VARCHAR(64) DEFAULT '',
        times_per_day   INTEGER DEFAULT 0,
        nurse_time_slots JSONB DEFAULT '[]',
        price           NUMERIC(10, 2) DEFAULT 0,
        is_required     BOOLEAN NOT NULL DEFAULT TRUE,
        risk_tier      VARCHAR(32) DEFAULT '',
        source          VARCHAR(16) NOT NULL DEFAULT 'template',
        active          BOOLEAN NOT NULL DEFAULT TRUE,
        tenant_id       VARCHAR(64) NOT NULL DEFAULT 'tenant_default',
        created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (site_id) REFERENCES sites(site_id)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_esi_site ON site_service_items(site_id, active)",
    "CREATE INDEX IF NOT EXISTS idx_esi_tenant ON site_service_items(tenant_id)",
    """
    CREATE TABLE IF NOT EXISTS daily_work_plans (
        plan_item_id    VARCHAR(64) PRIMARY KEY,
        site_id        VARCHAR(64) NOT NULL,
        plan_date       DATE NOT NULL,
        project_id      VARCHAR(64) DEFAULT '',
        project_name    VARCHAR(128) NOT NULL,
        nurse_time      VARCHAR(64) DEFAULT '',
        worker_id        VARCHAR(64) DEFAULT '',
        worker_name      VARCHAR(64) DEFAULT '',
        actual_care_time VARCHAR(64) DEFAULT '',
        status          VARCHAR(16) NOT NULL DEFAULT 'planned',
        tenant_id       VARCHAR(64) NOT NULL DEFAULT 'tenant_default',
        updated_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (site_id) REFERENCES sites(site_id)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_dcp_site_date ON daily_work_plans(site_id, plan_date DESC)",
    "CREATE INDEX IF NOT EXISTS idx_dcp_tenant ON daily_work_plans(tenant_id)",
    # v8：护理等级 → 照护项目模板（对齐机构 SaaS 的 /care/config 照护模板配置）
    # 说明：在生产形态下这份配置由 SaaS 维护（按 nurseLevelName 组织），
    # care 只在 CARE_PLAN_PROVIDER=local（本地/演示）时用它兜住"该等级该做哪些服务"。
    """
    CREATE TABLE IF NOT EXISTS service_tier_configs (
        config_id       VARCHAR(64) PRIMARY KEY,
        risk_tier      VARCHAR(32) NOT NULL,
        project_id      VARCHAR(64) DEFAULT '',
        project_name    VARCHAR(128) NOT NULL,
        frequency_id    VARCHAR(64) DEFAULT '',
        frequency_name  VARCHAR(64) DEFAULT '',
        times_per_day   INTEGER DEFAULT 0,
        nurse_time_slots JSONB DEFAULT '[]',
        price           NUMERIC(10, 2) DEFAULT 0,
        is_required     BOOLEAN NOT NULL DEFAULT TRUE,
        sort_order      INTEGER DEFAULT 0,
        active          BOOLEAN NOT NULL DEFAULT TRUE,
        tenant_id       VARCHAR(64) NOT NULL DEFAULT 'tenant_default',
        created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_clc_level ON service_tier_configs(risk_tier, active)",
]


def _load_v3_sql() -> list[str]:
    import pathlib
    p = pathlib.Path(__file__).parent / 'migrations_v3.sql'
    return [x.strip() for x in p.read_text().split(';') if x.strip()]


async def run_migrations():
    """启动时执行所有幂等迁移。单条失败只记日志，不阻断应用启动。"""
    applied, skipped = 0, 0
    for stmt in _load_v3_sql():
        _MIGRATIONS.append(stmt)
    async with AsyncSessionLocal() as session:
        for stmt in _MIGRATIONS:
            try:
                await session.execute(text(stmt))
                applied += 1
            except Exception as e:
                skipped += 1
                logger.warning("migrations.statement_failed",
                               stmt=stmt.strip().splitlines()[0][:80], error=str(e))
        await session.commit()
    logger.info("migrations.done", applied=applied, skipped=skipped)

    # 幂等播种设备类型字典（device_types 为空或新增类型时补齐，不覆盖已有记录）
    from backend.db.repositories import ensure_device_types
    await ensure_device_types()

    # 幂等播种护理等级照护模板（本地 Provider 的演示配置；生产由 SaaS /care/config 提供）
    from backend.db.repositories import ensure_service_tier_configs
    await ensure_service_tier_configs()

    # 幂等播种默认账号：**默认关闭**（SEED_DEMO_USERS=false）。
    # 开源项目不能一启动就带上"人人皆知的默认口令"；需要直接体验时显式开启，
    # 生产环境用 scripts/create_user.py 创建自己的管理员。
    from backend.config import get_settings
    if get_settings().seed_demo_users:
        from backend.db.repositories import ensure_default_users
        await ensure_default_users()
        logger.warning("migrations.demo_users_seeded",
                       message="已播种演示账号（worker/supervisor/admin），"
                               "仅用于本地演示，请勿在生产开启 SEED_DEMO_USERS")
    else:
        logger.info("migrations.demo_users_skipped",
                    message="未播种演示账号；用 scripts/create_user.py 创建用户")
