-- scripts/init_db.sql
-- FieldOps 数据库初始化脚本
-- 用法：psql -h localhost -p 5433 -U postgres -d fieldops -f scripts/init_db.sql

-- ──────────────────────────────────────────────────────────────
-- 点位信息表
-- ──────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS sites (
    site_id    VARCHAR(64) PRIMARY KEY,          -- 点位 ID
    name        VARCHAR(100) NOT NULL,           -- 点位名称，如「3号楼B1配电间」
    built_year  INTEGER,                         -- 建成年份（判断设施老化程度）
    site_type   VARCHAR(20),                     -- 点位类型：配电间/机房/仓库/公共区域
    location    VARCHAR(64),                     -- 位置，如「3号楼B1」
    risk_tier   VARCHAR(20) DEFAULT '三级',       -- 风险等级：三级(低)/二级(中)/一级(高)
    tenant_id   VARCHAR(64) NOT NULL DEFAULT 'tenant_default',  -- 租户 ID
    created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- ──────────────────────────────────────────────────────────────
-- 设备表
-- ──────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS devices (
    device_id    VARCHAR(64) PRIMARY KEY,        -- 设备 ID
    site_id     VARCHAR(64) NOT NULL,            -- 点位 ID（外键）
    device_type  VARCHAR(50) NOT NULL,            -- 设备类型名（历史自由字符串，兼容保留）
    type_code    VARCHAR(50),                     -- 设备类型编码（对齐 device_types.type_code）
    location     VARCHAR(128) DEFAULT '',         -- 安装位置（对齐 SaaS 的"设备安装位置"）
    status       VARCHAR(20) DEFAULT 'active',    -- 状态：active/inactive/maintenance
    tenant_id    VARCHAR(64) NOT NULL DEFAULT 'tenant_default',  -- 租户 ID
    created_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (site_id) REFERENCES sites(site_id)
);

-- 设备类型字典：编码对齐业务系统的设备类型字典
-- trust_level 是证据可信度（1-5），研判时按来源加权，避免四轨等权
CREATE TABLE IF NOT EXISTS device_types (
    type_code    VARCHAR(50) PRIMARY KEY,
    type_name    VARCHAR(64) NOT NULL,
    metrics      JSONB NOT NULL DEFAULT '[]',
    alert_types  JSONB NOT NULL DEFAULT '[]',
    trust_level  SMALLINT NOT NULL DEFAULT 3,
    trust_reason TEXT DEFAULT '',
    tenant_id    VARCHAR(64) NOT NULL DEFAULT 'tenant_default'
);

-- ──────────────────────────────────────────────────────────────
-- 告警记录表
-- ──────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS alerts (
    alert_id     VARCHAR(64) PRIMARY KEY,        -- 告警 ID
    site_id     VARCHAR(64) NOT NULL,            -- 点位 ID
    device_id    VARCHAR(64) NOT NULL,            -- 设备 ID
    alert_type   VARCHAR(50) NOT NULL,            -- 告警类型：heart_rate_abnormal/off_bed_timeout/fall_detected/room_temp_abnormal
    status       VARCHAR(20) DEFAULT 'pending',   -- 状态：pending/reviewing/dispatched/resolved/dismissed
    risk_level   VARCHAR(20),                      -- 风险等级：high/medium/low
    risk_assessment JSONB,                          -- AI 风险评估结果（assess_risk 节点产出）
    three_tracks    JSONB,                          -- 四轨采集数据（设备/服务/健康/最近一次风险评估，供 review 接口读取；列名沿用历史命名）
    source       VARCHAR(16) NOT NULL DEFAULT 'local',  -- 来源：local=本服务自触发 / saas=业务系统下发
    external_alert_id    VARCHAR(64),                       -- 业务系统的报警记录 ID（处置结果按它回写）
    location     VARCHAR(128) DEFAULT '',           -- 报警位置（对齐 报警记录 的"报警位置"）
    handled_by   VARCHAR(64) DEFAULT '',            -- 处理人（对齐 报警记录 的"处理人"）
    handled_at   TIMESTAMP,                          -- 处理时间（对齐 报警记录 的"处理时间"）
    tenant_id    VARCHAR(64) NOT NULL DEFAULT 'tenant_default',  -- 租户 ID
    created_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (site_id) REFERENCES sites(site_id),
    FOREIGN KEY (device_id) REFERENCES devices(device_id)
);

-- ──────────────────────────────────────────────────────────────
-- 服务计划表
-- ──────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS service_plans (
    plan_id      VARCHAR(64) PRIMARY KEY,        -- 计划 ID
    site_id     VARCHAR(64) NOT NULL,            -- 点位 ID
    plan_content JSONB,                            -- 计划内容（JSONB）
    tenant_id    VARCHAR(64) NOT NULL DEFAULT 'tenant_default',  -- 租户 ID
    created_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (site_id) REFERENCES sites(site_id)
);

-- ──────────────────────────────────────────────────────────────
-- 服务项（对齐业务系统的服务内容）：
-- 业务配置决定"要做什么项目、一天几次、在哪个时间窗、是否必选"，
-- AI 不参与这几项决策，只负责评估结论与异常处置建议。
-- ──────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS site_service_items (
    item_id         VARCHAR(64) PRIMARY KEY,
    site_id        VARCHAR(64) NOT NULL,
    project_id      VARCHAR(64) DEFAULT '',
    project_name    VARCHAR(128) NOT NULL,
    frequency_id    VARCHAR(64) DEFAULT '',
    frequency_name  VARCHAR(64) DEFAULT '',
    times_per_day   INTEGER DEFAULT 0,
    nurse_time_slots JSONB DEFAULT '[]',          -- [{"startTime":"08:00","endTime":"10:00"}]
    price           NUMERIC(10, 2) DEFAULT 0,     -- 服务费用（对齐 SaaS 的 price）
    is_required     BOOLEAN NOT NULL DEFAULT TRUE,-- 必选/选做（对齐 isRequired）
    risk_tier      VARCHAR(32) DEFAULT '',       -- 风险等级（对齐 nurseLevelName）
    source          VARCHAR(16) NOT NULL DEFAULT 'template',  -- template / manual
    active          BOOLEAN NOT NULL DEFAULT TRUE,
    tenant_id       VARCHAR(64) NOT NULL DEFAULT 'tenant_default',
    created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (site_id) REFERENCES sites(site_id)
);

-- ──────────────────────────────────────────────────────────────
-- 每日服务计划（对齐业务系统）：本服务侧只读。
-- 生成规则属于业务侧（每日计划由业务系统生成），本服务不重复实现。
-- ──────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS daily_work_plans (
    plan_item_id    VARCHAR(64) PRIMARY KEY,
    site_id        VARCHAR(64) NOT NULL,
    plan_date       DATE NOT NULL,
    project_id      VARCHAR(64) DEFAULT '',
    project_name    VARCHAR(128) NOT NULL,
    nurse_time      VARCHAR(64) DEFAULT '',       -- 作业时间（对齐 nurseTime）
    worker_id        VARCHAR(64) DEFAULT '',
    worker_name      VARCHAR(64) DEFAULT '',       -- 服务人员（对齐 workerName）
    actual_work_time VARCHAR(64) DEFAULT '',      -- 实际作业时间（对齐 actualWorkTime）
    status          VARCHAR(16) NOT NULL DEFAULT 'planned',  -- planned/done/missed
    tenant_id       VARCHAR(64) NOT NULL DEFAULT 'tenant_default',
    updated_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (site_id) REFERENCES sites(site_id)
);

-- ──────────────────────────────────────────────────────────────
-- 风险等级 → 服务项目模板（对齐业务系统的服务模板配置）
-- 生产形态下这份配置由 SaaS 按风险等级维护；本服务只在
-- SERVICE_PLAN_PROVIDER=local（本地/演示）时用它兜住"该等级该做哪些服务"。
-- ──────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS service_tier_configs (
    config_id       VARCHAR(64) PRIMARY KEY,
    risk_tier      VARCHAR(32) NOT NULL,          -- 风险等级：三级/二级/一级
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
);

-- ──────────────────────────────────────────────────────────────
-- 服务记录表
-- ──────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS service_records (
    record_id    VARCHAR(64) PRIMARY KEY,        -- 记录 ID
    site_id     VARCHAR(64) NOT NULL,            -- 点位 ID
    service_type    VARCHAR(50) NOT NULL,            -- 服务类型：巡检/设备检查/环境检查/消防设施检查
    content      TEXT,                             -- 服务内容
    worker_id VARCHAR(64) NOT NULL,            -- 作业人员 ID
    tenant_id    VARCHAR(64) NOT NULL DEFAULT 'tenant_default',  -- 租户 ID
    created_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (site_id) REFERENCES sites(site_id)
);

-- ──────────────────────────────────────────────────────────────
-- 评估结果表
-- ──────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS assessment_results (
    assessment_id    VARCHAR(64) PRIMARY KEY,    -- 评估 ID
    site_id         VARCHAR(64) NOT NULL,       -- 点位 ID
    dimension_scores JSONB,                        -- 各维度评分（JSONB）
    weighted_score   NUMERIC(5,2),                 -- 加权综合得分
    summary          JSONB,                        -- 评估总结（JSONB）
    tenant_id     VARCHAR(64) NOT NULL DEFAULT 'tenant_default',  -- 租户 ID
    created_at       TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (site_id) REFERENCES sites(site_id)
);

-- ──────────────────────────────────────────────────────────────
-- 作业流程报告表
-- ──────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS work_flow_reports (
    report_id     VARCHAR(64) PRIMARY KEY,       -- 报告 ID
    session_id    VARCHAR(64) NOT NULL UNIQUE,    -- 作业流程会话 ID
    site_id      VARCHAR(64) NOT NULL,           -- 点位 ID
    service_plan_id  VARCHAR(64) DEFAULT '',         -- 服务计划 ID
    stage_reached VARCHAR(50),                    -- 流程结束时所处阶段
    total_turns   INTEGER DEFAULT 0,              -- 总轮数
    report        JSONB,                           -- WorkReport 结构化报告
    tenant_id     VARCHAR(64) NOT NULL DEFAULT 'tenant_default',  -- 租户 ID
    created_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (site_id) REFERENCES sites(site_id)
);

-- ──────────────────────────────────────────────────────────────
-- 作业流程会话登记表
-- 业务侧权威记录：作业人员从点位详情页按 site_id 查进行中的流程会话，
-- 拿回 session_id 拼 thread_id 即可命中检查点续跑，不再依赖浏览器记住 URL。
-- ──────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS work_flow_sessions (
    session_id    VARCHAR(64) PRIMARY KEY,        -- 会话 ID（/start 生成，与检查点 thread_id 对应）
    site_id      VARCHAR(64) NOT NULL,           -- 点位 ID
    staff_id      VARCHAR(64) NOT NULL,           -- 发起作业人员 ID
    current_stage VARCHAR(50) NOT NULL,           -- 当前阶段（每轮对话后同步）
    total_turns   INTEGER DEFAULT 0,              -- 总轮数
    status        VARCHAR(20) DEFAULT 'in_progress', -- in_progress / finished
    tenant_id     VARCHAR(64) NOT NULL DEFAULT 'tenant_default',  -- 租户 ID
    started_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (site_id) REFERENCES sites(site_id)
);

-- ──────────────────────────────────────────────────────────────
-- 派发工单表
-- ──────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS dispatch_orders (
    order_id      VARCHAR(64) PRIMARY KEY,       -- 工单 ID
    alert_id      VARCHAR(64) NOT NULL,           -- 告警 ID
    site_id      VARCHAR(64) NOT NULL,           -- 点位 ID
    order_content TEXT,                            -- 工单内容
    status        VARCHAR(20) DEFAULT 'dispatched', -- 状态：dispatched/in_progress/completed/dismissed
    tenant_id     VARCHAR(64) NOT NULL DEFAULT 'tenant_default',  -- 租户 ID
    created_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (alert_id) REFERENCES alerts(alert_id),
    FOREIGN KEY (site_id) REFERENCES sites(site_id)
);

-- ──────────────────────────────────────────────────────────────
-- 出站通知：租户回调配置（SaaS 接收地址 + 签名密钥）
-- ──────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS tenant_webhooks (
    tenant_id     VARCHAR(64) PRIMARY KEY,                        -- 租户 ID（一租户一地址）
    url           VARCHAR(512) NOT NULL,                          -- SaaS 接收回调的 HTTP(S) 地址
    secret        VARCHAR(128) NOT NULL DEFAULT '',               -- HMAC-SHA256 签名密钥
    enabled       BOOLEAN NOT NULL DEFAULT TRUE,                  -- 关闭后事件只落库、不投递
    updated_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- ──────────────────────────────────────────────────────────────
-- 出站通知：事件发件箱（outbox，保证"至少一次"投递 + 可审计）
-- ──────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS outbound_events (
    event_id      VARCHAR(64) PRIMARY KEY,                        -- 事件 ID
    tenant_id     VARCHAR(64) NOT NULL DEFAULT 'tenant_default',  -- 租户 ID
    event_type    VARCHAR(64) NOT NULL,                           -- 事件类型，如 work_flow.finished
    aggregate_id  VARCHAR(64) NOT NULL,                           -- 幂等键主体（如 session_id）
    payload       JSONB NOT NULL,                                 -- 事件载荷（含报告）
    status        VARCHAR(20) NOT NULL DEFAULT 'pending',          -- pending / sent / failed
    attempts      INTEGER NOT NULL DEFAULT 0,                     -- 已尝试投递次数
    last_error    TEXT DEFAULT '',                                -- 最近一次失败原因
    next_retry_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,            -- 下次可投递时间（指数退避）
    created_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    sent_at       TIMESTAMP
);

-- ──────────────────────────────────────────────────────────────
-- 用户表（登录认证）
-- ──────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS users (
    user_id       VARCHAR(64) PRIMARY KEY,        -- 用户 ID
    username      VARCHAR(64) NOT NULL UNIQUE,    -- 登录名
    password_hash VARCHAR(100) NOT NULL,          -- bcrypt 哈希
    role          VARCHAR(20) NOT NULL,           -- 角色：worker/supervisor/admin
    tenant_id     VARCHAR(64) NOT NULL DEFAULT 'tenant_default',  -- 租户 ID
    created_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- ──────────────────────────────────────────────────────────────
-- 索引
-- ──────────────────────────────────────────────────────────────
CREATE INDEX IF NOT EXISTS idx_users_username ON users(username);
CREATE INDEX IF NOT EXISTS idx_devices_site      ON devices(site_id);
CREATE INDEX IF NOT EXISTS idx_alerts_site       ON alerts(site_id);
CREATE INDEX IF NOT EXISTS idx_alerts_status      ON alerts(status);
CREATE INDEX IF NOT EXISTS idx_service_plans_site   ON service_plans(site_id);
CREATE INDEX IF NOT EXISTS idx_service_records_site  ON service_records(site_id);
CREATE INDEX IF NOT EXISTS idx_service_records_worker ON service_records(worker_id);
CREATE INDEX IF NOT EXISTS idx_assessment_site  ON assessment_results(site_id);
CREATE INDEX IF NOT EXISTS idx_work_flow_reports_site ON work_flow_reports(site_id);
CREATE INDEX IF NOT EXISTS idx_work_flow_sessions_site   ON work_flow_sessions(site_id);
CREATE INDEX IF NOT EXISTS idx_dispatch_alert     ON dispatch_orders(alert_id);
CREATE INDEX IF NOT EXISTS idx_dispatch_site     ON dispatch_orders(site_id);
-- 出站通知：幂等键唯一（同类型同主体只落一条）+ 待投递扫描索引
CREATE UNIQUE INDEX IF NOT EXISTS uniq_outbound_event_agg ON outbound_events(event_type, aggregate_id);
CREATE INDEX IF NOT EXISTS idx_outbound_pending  ON outbound_events(status, next_retry_at);
CREATE INDEX IF NOT EXISTS idx_outbound_tenant   ON outbound_events(tenant_id);
-- 多租户隔离索引
CREATE INDEX IF NOT EXISTS idx_alerts_tenant      ON alerts(tenant_id);
CREATE INDEX IF NOT EXISTS idx_assessment_tenant  ON assessment_results(tenant_id);
CREATE INDEX IF NOT EXISTS idx_work_flow_tenant   ON work_flow_reports(tenant_id);
CREATE INDEX IF NOT EXISTS idx_sites_tenant      ON sites(tenant_id);
-- ──────────────────────────────────────────────────────────────
-- 设备读数（历史上只在启动迁移 migrations_v3.sql 里创建，导致全新环境首跑时
-- 这行索引报 "relation device_readings does not exist"——这行 ERROR 会让人以为建库失败。
-- 放回建表脚本，让 init_db.sql 自成完整。）
-- ──────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS device_readings (
    reading_id    VARCHAR(64) PRIMARY KEY,
    device_id     VARCHAR(64) NOT NULL,
    site_id       VARCHAR(64) NOT NULL,
    device_type   VARCHAR(50) NOT NULL,
    metric_name   VARCHAR(50) NOT NULL,
    reading_value VARCHAR(50) NOT NULL,
    reading_unit  VARCHAR(20) DEFAULT '',
    is_abnormal   BOOLEAN DEFAULT FALSE,
    recorded_at   TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    tenant_id     VARCHAR(64) NOT NULL DEFAULT 'tenant_default',
    FOREIGN KEY (device_id) REFERENCES devices(device_id),
    FOREIGN KEY (site_id) REFERENCES sites(site_id)
);
CREATE INDEX IF NOT EXISTS idx_dr_site_time ON device_readings(site_id, recorded_at DESC);

CREATE INDEX IF NOT EXISTS idx_dr_tenant          ON device_readings(tenant_id);
