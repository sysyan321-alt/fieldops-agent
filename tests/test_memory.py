# tests/test_memory.py
# 会话记忆测试：窗口裁剪、摘要触发判据、摘要是否真的写回 state。
#
# 为什么单独测：这三件事曾经"文档写了、代码没落地"——
#   ① trim_messages_to_window 只在 work_flow 用、且窗口是 6 轮，SOP 问答压根没裁；
#   ② should_trigger_summary 用取模判断，只有恰好 20/40… 条消息才触发；
#   ③ save_memory 算完摘要只打日志、不写回 state，下一轮拿到的仍是 None（白花一次 LLM 调用）。

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

from backend.agents.work_flow.nodes import save_memory_node as work_flow_save_memory
from backend.agents.sop_qa.nodes import save_memory_node as sop_qa_save_memory
from backend.core import memory as memory_mod
from backend.core.memory import should_trigger_summary, trim_messages_to_window


def _dialogue(turns: int) -> list:
    """造 turns 轮对话（1 轮 = 1 问 + 1 答）。"""
    msgs = []
    for i in range(turns):
        msgs.append(HumanMessage(content=f"第{i+1}问"))
        msgs.append(AIMessage(content=f"第{i+1}答"))
    return msgs


def test_trim_keeps_recent_window_and_system_message():
    """超出窗口时只留最近 10 轮；系统消息始终保留。"""
    messages = [SystemMessage(content="系统设定")] + _dialogue(15)
    trimmed = trim_messages_to_window(messages, window_size=10)

    assert isinstance(trimmed[0], SystemMessage), "系统消息被裁掉了"
    assert len(trimmed) == 1 + 20, "应保留 10 轮 = 20 条对话"
    assert trimmed[-1].content == "第15答"
    assert trimmed[1].content == "第6问", "应留下最近 10 轮（第 6~15 轮）"


def test_trim_noop_when_within_window():
    """没超窗口就原样返回，不做无谓裁剪。"""
    messages = _dialogue(3)
    assert trim_messages_to_window(messages, window_size=10) == messages


def test_should_trigger_summary_counts_new_turns():
    """判据是"距上次摘要新增了几轮"，不是取模。"""
    assert not should_trigger_summary(_dialogue(9)), "9 轮不该触发"
    assert should_trigger_summary(_dialogue(10)), "满 10 轮应触发"
    assert should_trigger_summary(_dialogue(11)), "超过阈值仍要触发（旧实现会漏）"
    # 已压缩覆盖 20 条后，再新增不足 10 轮不重复压缩
    assert not should_trigger_summary(_dialogue(14), covered_count=20)
    assert should_trigger_summary(_dialogue(20), covered_count=20)


def test_should_trigger_summary_ignores_empty_history():
    """空历史不触发（旧实现对 0 条消息返回 True）。"""
    assert not should_trigger_summary([])


async def test_sop_qa_save_memory_writes_summary_back(monkeypatch):
    """摘要必须写回 state，并记录覆盖条数，供下一轮判断。"""
    async def _fake_summary(messages, existing_summary=None):
        return "【摘要】点位有高血压，夜间易醒"

    monkeypatch.setattr("backend.agents.sop_qa.nodes.compress_to_summary", _fake_summary)
    messages = _dialogue(10)

    update = await sop_qa_save_memory({
        "session_id": "s1", "messages": messages,
        "should_summarize": True, "existing_summary": None,
        "answer": "答复", "answer_mode": "rag", "confidence": 0.9, "sources": ["sop"],
    })

    assert update["existing_summary"] == "【摘要】点位有高血压，夜间易醒"
    assert update["summary_message_count"] == len(messages)
    assert update["structured_output"]["answer"] == "答复"


async def test_sop_qa_save_memory_skips_when_not_needed(monkeypatch):
    """不需要压缩时不写 existing_summary（保持上一份摘要不动）。"""
    called = False

    async def _fake_summary(messages, existing_summary=None):
        nonlocal called
        called = True
        return "不该被调用"

    monkeypatch.setattr("backend.agents.sop_qa.nodes.compress_to_summary", _fake_summary)
    update = await sop_qa_save_memory({
        "session_id": "s2", "messages": _dialogue(3), "should_summarize": False,
    })

    assert not called, "未达阈值不该调用 LLM 做摘要"
    assert "existing_summary" not in update


async def test_work_flow_save_memory_writes_summary_back(monkeypatch):
    """作业流程同样写回摘要（它下一轮 generate_response 会读）。"""
    async def _fake_summary(messages, existing_summary=None):
        return "【摘要】作业流程进度：日常巡检第 3 轮"

    monkeypatch.setattr("backend.agents.work_flow.nodes.compress_to_summary", _fake_summary)
    messages = _dialogue(10)

    update = await work_flow_save_memory({
        "session_id": "c1", "messages": messages, "should_summarize": True,
        "existing_summary": "【旧摘要】",
    })

    assert update["existing_summary"].startswith("【摘要】")
    assert update["summary_message_count"] == len(messages)


async def test_summary_failure_does_not_break_flow(monkeypatch):
    """摘要失败只记日志：旧摘要继续用，不把异常抛给用户。"""
    async def _boom(messages, existing_summary=None):
        raise RuntimeError("上游超时")

    monkeypatch.setattr("backend.agents.sop_qa.nodes.compress_to_summary", _boom)
    update = await sop_qa_save_memory({
        "session_id": "s3", "messages": _dialogue(10), "should_summarize": True,
        "existing_summary": "【旧摘要】", "answer": "答复",
    })

    assert "existing_summary" not in update, "失败时不应覆盖旧摘要"
    assert update["structured_output"]["answer"] == "答复"


class _FakeLLMResponse:
    """只提供节点会用到的 content / text。"""

    def __init__(self, text: str):
        self.content = text

    @property
    def text(self) -> str:
        return self.content


class _FakeLLM:
    def __init__(self, text: str):
        self._text = text

    async def ainvoke(self, _messages):
        return _FakeLLMResponse(self._text)

    async def astream(self, _messages):
        yield _FakeLLMResponse(self._text)


async def test_summary_survives_next_turn_within_same_thread(monkeypatch):
    """端到端：第 1 轮写回的摘要要能在第 2 轮读到（这就是"记得住"）。

    用真图 + 内存检查点 + 打桩 LLM，避免依赖网络与数据库。

    两个坑记在这里，避免以后又踩：
      ① 打补丁要打图模块里的名字（`sop_qa.graph.classify_query_node`），
         打 `sop_qa.nodes.classify_query_node` 是无效的——graph.py 在模块顶层
         import 了那个函数，`add_node` 用的是它自己模块里的绑定。
      ② 一旦分类没被打桩，Query 类型会落到 PRECISE 并走进检索链路，
         本机有模型权重时只是慢 1 秒，**没有权重时会去 HuggingFace 下载约 2GB**——
         测试就变成了对"作者本机环境"的依赖。所以这里额外兜一层：把检索也打桩。
    """
    from backend.agents.sop_qa import nodes as sop_nodes
    from backend.agents.sop_qa import graph as sop_graph
    from backend.agents.sop_qa.graph import build_sop_qa_graph
    from backend.config import get_settings
    from backend.core.memory import build_config, close_memory_savers, get_memory_saver

    monkeypatch.setattr(get_settings(), "checkpoint_backend", "memory", raising=False)
    await close_memory_savers()

    async def _fake_classify(_state):
        return {"query_type": "GENERAL"}          # 闲聊分支：不检索、不涉及向量库

    # ① 打图模块里的名字（这样图上挂的才是假函数）
    monkeypatch.setattr(sop_graph, "classify_query_node", _fake_classify)
    monkeypatch.setattr(sop_nodes, "get_llm",
                        lambda *_a, **_k: _FakeLLM("好的，我记住了。"))
    monkeypatch.setattr(sop_nodes, "compress_to_summary",
                        lambda messages, existing_summary=None: _async_summary(messages))

    # ② 兜底：即使分类被绕过，也不允许触发真实检索（否则会加载/下载模型）
    async def _no_retrieval(*_a, **_k):
        raise AssertionError("本用例不应走到检索链路（会把测试变成对模型权重的依赖）")

    monkeypatch.setattr("backend.core.retrieval_cache.cached_retrieve", _no_retrieval)

    graph = await build_sop_qa_graph()
    config = build_config("e-mem", "sess-mem", agent_type="sop_qa")
    base = {"tenant_id": "tenant_default", "site_id": "e-mem", "session_id": "sess-mem",
            "enable_web_search": False}

    # 第 1 轮：带 20 条历史 → 触发压缩
    turn1 = await graph.ainvoke(
        {**base, "original_query": "你好", "messages": _dialogue(10)},
        config=config,
    )
    assert turn1.get("existing_summary"), "第 1 轮没有写回摘要"
    assert turn1.get("summary_message_count") == len(turn1.get("messages", []))

    # 第 2 轮：同 thread 再问一次，摘要应仍在（随检查点保留）
    turn2 = await graph.ainvoke(
        {**base, "original_query": "再说一次", "messages": []},
        config=config,
    )
    assert turn2.get("existing_summary") == turn1["existing_summary"], "第 2 轮读不到上一轮的摘要"

    await close_memory_savers()


async def _async_summary(messages) -> str:
    await memory_mod.asyncio.sleep(0)
    return f"【摘要】已压缩 {len(messages)} 条消息"
