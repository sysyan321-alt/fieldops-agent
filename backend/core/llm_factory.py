# backend/core/llm_factory.py
# LLM Factory：统一封装大模型调用，按 Agent 类型路由。
# 规矩：所有 Agent 必须通过此模块获取模型，禁止直接调用 init_chat_model。

from typing import Type, Any                          # 类型注解用：Type 表示「某个类本身」，Any 表示任意类型
from pydantic import BaseModel                        # 结构化输出的 Schema 都是它的子类
import httpx                                          # HTTP 客户端库（用来自定义网络行为）
from langchain.chat_models import init_chat_model     # 创建聊天模型（1.x 写法）
from langchain_core.language_models import BaseChatModel  # 聊天模型的基类（类型注解用）
from langchain_core.runnables import Runnable         # 「可运行对象」基类，结构化模型属于它

from backend.config import get_settings               # 读配置（API Key、base_url 等）
from backend.core.logger import get_logger             # 结构化日志

logger = get_logger(__name__)

# ── 自定义 httpx 客户端：绕过系统代理 ───────────────────────────
# 背景：系统代理或 HTTPS_PROXY 环境变量会被 httpx 默认探测到，
#       导致 DeepSeek 请求经代理后 TLS 握手失败。DeepSeek 国内可直连，无需代理。
# trust_env=False 表示：完全忽略系统代理和相关环境变量。
_HTTP_ASYNC_CLIENT = httpx.AsyncClient(               # 异步客户端（给 ainvoke/astream 用）
    trust_env=False,
    timeout=httpx.Timeout(120.0, connect=15.0),       # 总超时 120 秒，建立连接超时 15 秒
)
_HTTP_SYNC_CLIENT = httpx.Client(                     # 同步客户端（给 invoke 用）
    trust_env=False,
    timeout=httpx.Timeout(120.0, connect=15.0),
)

# ── Agent 类型 → 提供商 路由表 ──────────────────────────────
# 只存 provider（deepseek / qwen），具体 model_name 从 .env.local 动态读取：
#   qwen     → settings.qwen_model_chat（QWEN_MODEL_CHAT）
#   deepseek → settings.deepseek_model_chat（DEEPSEEK_MODEL_CHAT）
# 想换模型，改 .env.local 的 QWEN_MODEL_CHAT / DEEPSEEK_MODEL_CHAT 即可，无需动代码。
_AGENT_MODEL_ROUTING: dict[str, str] = {
    "assessment":  "qwen",     # 老年人能力评估
    "sop_qa":      "qwen",     # 护理 SOP 问答
    "alert":       "qwen",     # 告警研判
    "care_flow":   "qwen",     # 照护流程
    "intent":      "qwen",     # 意图识别
    "summarize":   "qwen",     # 对话摘要压缩
}


class LLMFactory:
    """大模型工厂（统一获取模型的唯一入口）。
    用 @classmethod 定义方法，意味着不用创建对象、直接用 LLMFactory.get_llm(...) 调用。

    用法：
        llm = LLMFactory.get_llm("assessment")                              # 普通模型
        structured = LLMFactory.get_structured_llm("assessment", 某Schema)  # 结构化输出模型
        response = await llm.ainvoke(messages)
    """

    _instances: dict[str, BaseChatModel] = {}   # 类变量：模型实例缓存（缓存键 → 模型），全类共享

    @classmethod
    def _get_settings(cls):
        """内部小工具：取配置对象。"""
        return get_settings()


    @classmethod
    def _build_model_kwargs(cls, provider: str, model_name: str) -> dict[str, Any]:
        """组装 init_chat_model 需要的所有参数。
        provider: deepseek / qwen，两者都走 OpenAI 兼容接口。"""
        settings = cls._get_settings()

        if provider == "qwen":
            api_key = settings.qwen_api_key
            base_url = settings.qwen_base_url
        else:  # deepseek
            api_key = settings.deepseek_api_key
            base_url = settings.deepseek_base_url

        return {
            "model": model_name,
            "model_provider": "openai",            # 两者都走 langchain-openai
            "temperature": 0,
            "api_key": api_key,
            "base_url": base_url,
            "max_retries": 0,
            "http_async_client": _HTTP_ASYNC_CLIENT,
            "http_client": _HTTP_SYNC_CLIENT,
        }

    @classmethod
    def get_llm(
        cls,
        agent_type: str,             # Agent 类型，必须在路由表里
        temperature: float = 0,      # 温度：对话类可传 0.3~0.7，评分类保持 0
        streaming: bool = False,     # 是否流式输出（问答/照护流程对话用）
    ) -> BaseChatModel:
        """按 Agent 类型获取模型实例（带缓存）。
        相同 (模型, 温度, 是否流式) 的组合只会创建一次，之后复用。"""
        if agent_type not in _AGENT_MODEL_ROUTING:        # 校验：不认识的类型直接报错
            raise ValueError(
                f"未知 agent_type: '{agent_type}'，"
                f"可用类型：{list(_AGENT_MODEL_ROUTING.keys())}"
            )
        # provider 直接来自路由表，model_name 从 .env.local 动态读取
        provider = _AGENT_MODEL_ROUTING[agent_type]
        settings = cls._get_settings()
        if provider == "qwen":
            model_name = settings.qwen_model_chat
        else:  # deepseek
            model_name = settings.deepseek_model_chat
        # 用「provider_model_温度_是否流式」拼一个缓存键
        cache_key = f"{provider}_{model_name}_{temperature}_{streaming}"
        if cache_key not in cls._instances:  # 缓存里没有才新建
            kwargs = cls._build_model_kwargs(provider, model_name)  # 组装基础参数
            kwargs["temperature"] = temperature           # 覆盖温度
            kwargs["streaming"] = streaming               # 设置是否流式
            llm = init_chat_model(**kwargs)               # 真正创建模型
            cls._instances[cache_key] = llm               # 存进缓存
            logger.info(
                "llm_factory.model_initialized",
                agent_type=agent_type, provider=provider,
                model_name=model_name,
                temperature=temperature, streaming=streaming,
            )
        return cls._instances[cache_key]

    @classmethod
    def get_structured_llm(
        cls,
        agent_type: str,
        output_schema: Type[BaseModel],   # 期望的输出结构（一个 Pydantic 模型类）
        temperature: float = 0,
    ) -> Runnable:
        """获取「绑定了结构化输出 Schema」的模型。
        调用它的 ainvoke 后，直接返回一个 output_schema 类型的对象（不是文本）。"""
        llm = cls.get_llm(agent_type, temperature=temperature)
        # 绑定 Pydantic 结构；method="function_calling" 是 DeepSeek 必须的
        return llm.with_structured_output(output_schema, method="function_calling")

    @classmethod
    def clear_cache(cls) -> None:
        """清空模型实例缓存（测试时用）。"""
        cls._instances.clear()
        logger.info("llm_factory.cache_cleared")


# ── 模块级便捷函数（Agent 代码里的推荐写法）────────────────────────

def get_llm(agent_type: str, temperature: float = 0, streaming: bool = False) -> BaseChatModel:
    """LLMFactory.get_llm 的便捷入口。"""
    return LLMFactory.get_llm(agent_type, temperature=temperature, streaming=streaming)


def get_structured_llm(agent_type: str, output_schema: Type[BaseModel]) -> Runnable:
    """LLMFactory.get_structured_llm 的便捷入口。"""
    return LLMFactory.get_structured_llm(agent_type, output_schema)
