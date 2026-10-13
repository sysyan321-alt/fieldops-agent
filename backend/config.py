# backend/config.py
# 全项目唯一的「配置中心」：从 .env.local 读取所有配置项，供任何模块取用。

from pydantic_settings import BaseSettings   # Pydantic 的「配置基类」，能自动从环境变量/.env 读取并做类型校验
from functools import lru_cache              # 标准库装饰器：缓存函数结果，让函数实际只执行一次
import os, sys
live_path = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

env_local_path = os.path.join(live_path, ".env.local")


class Settings(BaseSettings):
    """配置模型：每个类属性对应 .env.local 里的一项配置。
    继承 BaseSettings 后，Pydantic 会自动把同名（大小写不敏感）的配置读进来并转成对应类型。"""

    # ── 数据库（PostgreSQL）──
    db_host: str = "localhost"   # 主机；写了默认值 = 可选项
    db_port: int = 5433          # 端口；本机 5432 已占用，隔离到 5433
    db_name: str = "careagent"   # 库名（养老照护智能体）
    db_user: str                 # 用户名；没有默认值 = 必填，.env.local 缺了会启动报错
    db_password: str             # 密码；同样必填

    @property
    def database_url(self) -> str:
        """把上面几个散件拼成 SQLAlchemy 需要的连接串。
        用 @property 装饰后，可像访问属性一样 settings.database_url 取值，不用加括号调用。"""
        return (
            f"postgresql+asyncpg://{self.db_user}:{self.db_password}"
            f"@{self.db_host}:{self.db_port}/{self.db_name}"
        )

    @property
    def checkpoint_dsn(self) -> str:
        """LangGraph checkpointer 用的连接串（psycopg 直连，不带 +asyncpg 驱动前缀）。

        和 database_url 指向同一个库，只是驱动不同：业务侧走 SQLAlchemy + asyncpg，
        检查点侧走 psycopg（langgraph-checkpoint-postgres 的官方实现）。
        密码里的特殊字符做 URL 编码，避免 @ / : 把连接串解析歪。
        """
        from urllib.parse import quote_plus
        return (
            f"postgresql://{quote_plus(self.db_user)}:{quote_plus(self.db_password)}"
            f"@{self.db_host}:{self.db_port}/{self.db_name}"
        )

    # ── 会话检查点（LangGraph checkpointer）──
    # auto     = 优先 PostgreSQL，连不上时回落进程内内存（开发 / 离线可用）
    # postgres = 强制 PostgreSQL，连不上直接报错（生产用，避免静默丢状态）
    # memory   = 强制内存（单测 / 无库环境）
    checkpoint_backend: str = "auto"
    checkpoint_pool_min_size: int = 1
    checkpoint_pool_max_size: int = 10
    checkpoint_setup_on_start: bool = True   # 启动时建表（checkpoints / blobs / writes）

    # ── Milvus 向量库 ──
    milvus_host: str = "localhost"
    milvus_port: int = 19531     # 本机 19530 已占用，隔离到 19531

    # ── Redis 缓存 ──
    redis_host: str = "localhost"
    redis_port: int = 6380       # 本机 6379 已占用，隔离到 6380
    redis_db: int = 0
    cache_enabled: bool = True   # 关掉则缓存层完全旁路（fail-open）
    cache_ttl_intent: int = 600      # 意图识别结果缓存 10 分钟
    cache_ttl_retrieval: int = 300   # SOP 检索结果缓存 5 分钟

    # ── 大模型（DeepSeek，OpenAI 兼容接口）──
    deepseek_api_key: str                                   # 必填：DeepSeek 的 API Key
    deepseek_base_url: str = "https://api.deepseek.com/v1"  # DeepSeek 接口地址
    deepseek_model_chat: str = "deepseek-chat"              # 对话模型名

    # ── 大模型（Qwen/通义千问，DashScope OpenAI 兼容接口）──
    qwen_api_key: str = ""                                  # Qwen API Key
    qwen_base_url: str = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    qwen_model_chat: str = "deepseek-v4.1-flash"             # 对话模型名

    # ── 默认模型提供商：deepseek / qwen ──
    default_llm_provider: str = "qwen"                      # 当前默认用 Qwen

    # ── 本地模型权重路径 ──
    reranker_model_path: str = "./models/reranker/bge-reranker-large"    # 精排模型
    bge_m3_model_path: str = "./models/embedding/bge-m3"                 # 嵌入模型

    # ── JWT 认证 ──
    jwt_secret_key: str                           # 必填：签发登录令牌用的密钥
    jwt_algorithm: str = "HS256"                  # 签名算法
    jwt_access_token_expire_minutes: int = 10080  # 令牌有效期（分钟），默认 7 天

    # ── Web 搜索（Tavily 可选；留空则自动用免费的 DuckDuckGo）──
    tavily_api_key: str = ""

    # ── 应用基础配置 ──
    app_env: str = "local"                     # 运行环境标识
    app_debug: bool = False                    # 是否调试模式
    app_host: str = "0.0.0.0"                  # 监听地址
    app_port: int = 8000                       # 监听端口
    log_level: str = "INFO"                    # 日志级别
    default_tenant_id: str = "tenant_default"  # 多租户默认值

    # ── CORS ──
    # 逗号分隔的来源白名单；"*" 表示允许任意来源（开发/演示默认）。
    # 注意：本项目用 Bearer Token 鉴权，不依赖 Cookie，所以默认不需要
    # allow_credentials —— 而 allow_origins=["*"] 与 allow_credentials=True 是
    # 无效组合（浏览器会直接拒绝），见 main.py 里的守卫。
    cors_origins: str = "*"
    cors_allow_credentials: bool = False

    # ── 登录保护 ──
    login_max_attempts: int = 5                # 窗口内允许的失败次数
    login_lockout_seconds: int = 300           # 超过后锁定时长（秒）

    # ── 演示数据 ──
    # 默认关闭：开源项目不能一启动就带上"人人皆知的默认口令"。
    # 需要直接体验时置 true（会播种 caregiver/supervisor/admin 三个演示账号），
    # 生产环境请用 scripts/create_user.py 创建自己的管理员。
    seed_demo_users: bool = False

    # ── 出站通知（Agent → 外部 SaaS 回调）──
    # 业务事件（如照护流程终态报告）先落发件箱 outbound_events，
    # 再由后台投递器 POST 到租户在 tenant_webhooks 里登记的回调地址。
    outbound_enabled: bool = True              # 是否随应用启动后台投递器
    outbound_worker_interval: int = 15         # 投递轮询间隔（秒）
    outbound_batch_size: int = 20              # 单轮最多投递条数
    outbound_timeout: float = 10.0             # 单次 HTTP 投递超时（秒）

    # ── 照护计划数据来源（Provider 抽象，见 backend/core/care_plan_provider.py）──
    # local = 读 care 自己的库（demo / 离线可跑）
    # saas  = 读机构 SaaS（需要 saas_care_base_url；取不到时回落 local 并告警）
    care_plan_provider: str = "local"
    saas_care_base_url: str = ""               # 机构 SaaS 地址，如 http://sass.example.com/api
    saas_care_token: str = ""                  # 主系统鉴权令牌（按需）

    # ── MCP（Model Context Protocol）能力层 ──
    mcp_enabled: bool = True                   # 是否把 MCP Server 挂载进主应用（/mcp/*）
    mcp_admin_token: str = ""                  # 写工具（alert-ops / sop-kb-admin）的 Bearer Token；留空=写工具禁用
    mcp_write_tenants: str = "tenant_default"  # 写工具允许操作的租户白名单（逗号分隔）

    class Config:
        """Pydantic 的元配置：告诉 BaseSettings 该怎么读取配置。"""
        env_file = env_local_path          # 从这个文件读取配置
        env_file_encoding = "utf-8"      # 文件编码
        case_sensitive = False           # 大小写不敏感：环境变量 DB_HOST 能对应字段 db_host
        extra = "ignore"                 # .env.local 里多出来的、模型没定义的字段一律忽略（不报错）


@lru_cache()                             # 缓存：保证 get_settings() 只创建一次 Settings、只读一次文件
def get_settings() -> Settings:
    """获取全局唯一的配置对象。任何模块要用配置，都调用这个函数。"""
    return Settings()                    # 首次调用时创建实例；之后每次都返回同一个缓存对象


if __name__ == '__main__':
    settings = get_settings()
    print(settings.database_url)
    print(settings.db_user)
