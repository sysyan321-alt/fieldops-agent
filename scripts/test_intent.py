# scripts/test_intent.py
# 意图识别升级测试：验证 LLM 驱动 + 三层 fallback + clarify 分支
#
# 覆盖：
#   1. 4 个 Agent 直达用例（assessment/sop_qa/alert/care_flow）
#   2. 跨域冲突样本（"评估跌倒风险" → SOP_QA 而非 ASSESSMENT）
#   3. 闲聊兜底（"你是谁" → SOP_QA）
#   4. LLM 失败 fallback（mock 抛异常 → 关键词降级）
#   5. needs_clarification 触发（跨域无法判断主意图）
#   6. orchestrator.execute 端到端验证（含 clarify 分支）
#
# 用法：cd <项目根目录> && PYTHONPATH=. python scripts/test_intent.py

import asyncio
from unittest.mock import patch, AsyncMock

from backend.core.intent import (
    IntentClassification,
    IntentAgentType,
    classify_intent,
)
from backend.core.orchestrator import get_orchestrator, AgentRequest, ExecutionMode
from backend.core.logger import configure_logging, get_logger

configure_logging()
logger = get_logger(__name__)


# ──────────────────────────────────────────────────────────────
# 测试用例定义
# ──────────────────────────────────────────────────────────────

# 1. 4 个 Agent 直达用例
DIRECT_CASES = [
    ("对老人做能力评估",                    IntentAgentType.ASSESSMENT, "评估动作"),
    ("鼻饲管日常怎么维护",                  IntentAgentType.SOP_QA,     "护理 SOP"),
    ("301 房老人心率 130 触发告警",         IntentAgentType.ALERT,      "实时告警"),
    ("明天执行入住流程第三阶段",            IntentAgentType.CARE_FLOW,  "流程执行"),
]

# 2. 跨域冲突样本（关键词匹配会错，LLM 应正确判定）
CONFLICT_CASES = [
    # (query, expected_agent, description)
    ("评估跌倒风险",   IntentAgentType.SOP_QA, "问的是跌倒预防知识，不是要做评估"),
    ("跌倒后怎么处理", IntentAgentType.SOP_QA, "问护理处置 SOP，不是告警事件"),
    ("评估老人的营养状况", IntentAgentType.ASSESSMENT, "明确是评估动作"),
]

# 3. 闲聊兜底
CHITCHAT_CASES = [
    ("你是谁",     IntentAgentType.SOP_QA, "闲聊"),
    ("今天天气怎么样", IntentAgentType.SOP_QA, "非业务兜底"),
    ("你好",       IntentAgentType.SOP_QA, "问候"),
]


# ──────────────────────────────────────────────────────────────
# 测试 1-3：LLM 正常路径
# ──────────────────────────────────────────────────────────────

async def test_direct_cases():
    """测试 1：4 个 Agent 直达用例。"""
    print("\n" + "=" * 60)
    print("  测试 1：4 个 Agent 直达用例（LLM 正常路径）")
    print("=" * 60)

    passed = 0
    for query, expected, desc in DIRECT_CASES:
        result = await classify_intent(query)
        ok = result.agent_type == expected and not result.needs_clarification
        status = "✅" if ok else "❌"
        print(f"\n  {status} [{desc}] query='{query}'")
        print(f"     expected={expected.value}, got={result.agent_type.value}")
        print(f"     confidence={result.confidence:.2f}, clarify={result.needs_clarification}")
        print(f"     reasoning={result.reasoning[:80]}")
        if ok:
            passed += 1

    print(f"\n  结果：{passed}/{len(DIRECT_CASES)} 通过")
    return passed == len(DIRECT_CASES)


async def test_conflict_cases():
    """测试 2：跨域冲突样本（LLM 应正确判定，关键词会错）。"""
    print("\n" + "=" * 60)
    print("  测试 2：跨域冲突样本")
    print("=" * 60)

    passed = 0
    for query, expected, desc in CONFLICT_CASES:
        result = await classify_intent(query)
        ok = result.agent_type == expected
        status = "✅" if ok else "❌"
        print(f"\n  {status} [{desc}] query='{query}'")
        print(f"     expected={expected.value}, got={result.agent_type.value}")
        print(f"     confidence={result.confidence:.2f}")
        print(f"     reasoning={result.reasoning[:80]}")
        if ok:
            passed += 1

    print(f"\n  结果：{passed}/{len(CONFLICT_CASES)} 通过")
    return passed == len(CONFLICT_CASES)


async def test_chitchat_cases():
    """测试 3：闲聊/非业务兜底。"""
    print("\n" + "=" * 60)
    print("  测试 3：闲聊兜底")
    print("=" * 60)

    passed = 0
    for query, expected, desc in CHITCHAT_CASES:
        result = await classify_intent(query)
        ok = result.agent_type == expected
        status = "✅" if ok else "❌"
        print(f"\n  {status} [{desc}] query='{query}'")
        print(f"     expected={expected.value}, got={result.agent_type.value}")
        print(f"     confidence={result.confidence:.2f}")
        if ok:
            passed += 1

    print(f"\n  结果：{passed}/{len(CHITCHAT_CASES)} 通过")
    return passed == len(CHITCHAT_CASES)


# ──────────────────────────────────────────────────────────────
# 测试 4：LLM 失败 fallback（mock 抛异常 → 关键词降级）
# ──────────────────────────────────────────────────────────────

async def test_llm_failure_fallback():
    """测试 4：LLM 调用失败时降级到关键词匹配。"""
    print("\n" + "=" * 60)
    print("  测试 4：LLM 失败 fallback（mock 抛异常）")
    print("=" * 60)

    # 构造一个会抛异常的 mock structured_llm
    async def _raise_llm_error(*_args, **_kwargs):
        raise RuntimeError("mock LLM API failure")

    passed = 0
    total = 0

    # 4-1: 关键词能命中 → 应降级到对应 Agent（confidence <= 0.5）
    keyword_cases = [
        ("请对老人做能力评估",  IntentAgentType.ASSESSMENT, "评估"),
        ("301 房心率异常告警", IntentAgentType.ALERT,      "告警"),
        ("明天执行入住流程",    IntentAgentType.CARE_FLOW,  "入住流程"),
    ]
    for query, expected, kw in keyword_cases:
        total += 1
        with patch("backend.core.intent.get_structured_llm") as mock_get:
            # 让 get_structured_llm 返回一个 ainvoke 会抛异常的对象
            mock_llm = AsyncMock()
            mock_llm.ainvoke = _raise_llm_error
            mock_get.return_value = mock_llm
            result = await classify_intent(query)
        ok = (
            result.agent_type == expected
            and result.confidence <= 0.5   # 降级标记
            and "keyword_fallback" in result.reasoning
        )
        status = "✅" if ok else "❌"
        print(f"\n  {status} [关键词命中: {kw}] query='{query}'")
        print(f"     expected={expected.value}, got={result.agent_type.value}")
        print(f"     confidence={result.confidence:.2f} (应 <=0.5)")
        print(f"     reasoning={result.reasoning}")
        if ok:
            passed += 1

    # 4-2: 关键词也不命中 → SOP_QA 兜底
    total += 1
    with patch("backend.core.intent.get_structured_llm") as mock_get:
        mock_llm = AsyncMock()
        mock_llm.ainvoke = _raise_llm_error
        mock_get.return_value = mock_llm
        result = await classify_intent("随便说点啥不命中关键词的句子")
    ok = (
        result.agent_type == IntentAgentType.SOP_QA
        and result.confidence <= 0.3   # 兜底置信度
        and "默认兜底" in result.reasoning
    )
    status = "✅" if ok else "❌"
    print(f"\n  {status} [关键词均不命中 → SOP_QA 兜底]")
    print(f"     got={result.agent_type.value}, confidence={result.confidence:.2f}")
    print(f"     reasoning={result.reasoning}")
    if ok:
        passed += 1

    print(f"\n  结果：{passed}/{total} 通过")
    return passed == total


# ──────────────────────────────────────────────────────────────
# 测试 5：needs_clarification 触发
# ──────────────────────────────────────────────────────────────

async def test_clarification_trigger():
    """测试 5：跨域无法判断主意图 → needs_clarification=True + post-processing。

    覆盖：
      5-1: 正常 2 候选 → 保持不变
      5-2: LLM 只给 1 候选（场景 D 的真实 bug）→ post-process 补 SOP_QA 凑够 2 个
      5-3: LLM 给 0 候选 + clarify=True → post-process 降级为非 clarify
      5-4: LLM 给 1 候选 = agent_type（无其他候选）→ post-process 降级为非 clarify
    """
    print("\n" + "=" * 60)
    print("  测试 5：needs_clarification 触发 + post-processing 兜底")
    print("=" * 60)

    passed = 0
    total = 0

    async def _run_mock(mock_result, query, tag):
        nonlocal passed, total
        with patch("backend.core.intent.get_structured_llm") as mock_get:
            mock_llm = AsyncMock()
            mock_llm.ainvoke = AsyncMock(return_value=mock_result)
            mock_get.return_value = mock_llm
            return await classify_intent(query)

    # 5-1: 正常 2 候选 → 保持
    total += 1
    mock1 = IntentClassification(
        agent_type=IntentAgentType.ASSESSMENT,
        confidence=0.4,
        needs_clarification=True,
        candidates=[IntentAgentType.ASSESSMENT, IntentAgentType.CARE_FLOW],
        reasoning="跨域：评估 + 照护流程",
    )
    r1 = await _run_mock(mock1, "评估照护流程", "5-1")
    ok1 = (
        r1.needs_clarification is True
        and len(r1.candidates) == 2
        and IntentAgentType.ASSESSMENT in r1.candidates
        and IntentAgentType.CARE_FLOW in r1.candidates
    )
    print(f"\n  {'✅' if ok1 else '❌'} [5-1] 正常 2 候选 → 保持不变")
    print(f"     needs_clarification={r1.needs_clarification}, candidates={[c.value for c in r1.candidates]}")
    if ok1: passed += 1

    # 5-2: LLM 只给 1 候选（场景 D 的真实 bug）→ post-process 补 SOP_QA
    total += 1
    mock2 = IntentClassification(
        agent_type=IntentAgentType.ASSESSMENT,
        confidence=0.4,
        needs_clarification=True,
        candidates=[IntentAgentType.ASSESSMENT],   # 只有 1 个，违反约束
        reasoning="跨域：评估 + 照护流程，无法判断主意图",
    )
    r2 = await _run_mock(mock2, "评估照护流程", "5-2")
    ok2 = (
        r2.needs_clarification is True
        and len(r2.candidates) >= 2          # post-process 补到 >=2
        and IntentAgentType.ASSESSMENT in r2.candidates
        and any(c != IntentAgentType.ASSESSMENT for c in r2.candidates)  # 至少有 1 个其他
    )
    print(f"\n  {'✅' if ok2 else '❌'} [5-2] LLM 只给 1 候选 → post-process 补足到 ≥2")
    print(f"     原始 candidates=['assessment']（违规）")
    print(f"     修复后 candidates={[c.value for c in r2.candidates]}")
    if ok2: passed += 1

    # 5-3: LLM 给 candidates 不含 agent_type → 补 agent_type 到候选首
    total += 1
    mock3 = IntentClassification(
        agent_type=IntentAgentType.ASSESSMENT,
        confidence=0.4,
        needs_clarification=True,
        candidates=[IntentAgentType.CARE_FLOW],   # 不含 agent_type
        reasoning="跨域：评估 + 照护流程",
    )
    r3 = await _run_mock(mock3, "评估照护流程", "5-3")
    ok3 = (
        r3.needs_clarification is True
        and len(r3.candidates) == 2
        and IntentAgentType.ASSESSMENT in r3.candidates
        and IntentAgentType.CARE_FLOW in r3.candidates
    )
    print(f"\n  {'✅' if ok3 else '❌'} [5-3] candidates 不含 agent_type → 补 agent_type 到候选")
    print(f"     原始 candidates=['care_flow']（缺 agent_type）")
    print(f"     修复后 candidates={[c.value for c in r3.candidates]}")
    if ok3: passed += 1

    # 5-4: agent_type=SOP_QA 且 candidates=[SOP_QA] → 无法补 SOP_QA 凑数 → 降级
    total += 1
    mock4 = IntentClassification(
        agent_type=IntentAgentType.SOP_QA,
        confidence=0.4,
        needs_clarification=True,
        candidates=[IntentAgentType.SOP_QA],   # 唯一候选 = agent_type = SOP_QA
        reasoning="跨域但只指向 SOP_QA 兜底",
    )
    r4 = await _run_mock(mock4, "随便聊聊", "5-4")
    ok4 = (
        r4.needs_clarification is False
        and len(r4.candidates) == 0
        and "降级" in r4.reasoning
    )
    print(f"\n  {'✅' if ok4 else '❌'} [5-4] agent_type=SOP_QA 且无其他候选 → 降级（无法补 SOP_QA 凑数）")
    print(f"     needs_clarification={r4.needs_clarification}")
    print(f"     reasoning={r4.reasoning}")
    if ok4: passed += 1

    print(f"\n  结果：{passed}/{total} 通过")
    return passed == total


# ──────────────────────────────────────────────────────────────
# 测试 6：orchestrator.execute 端到端（含 clarify 分支）
# ──────────────────────────────────────────────────────────────

async def test_orchestrator_execute_clarify():
    """测试 6：orchestrator.execute 处理 needs_clarification=True 分支。"""
    print("\n" + "=" * 60)
    print("  测试 6：orchestrator.execute → clarify 分支")
    print("=" * 60)

    # mock 意图识别返回 clarify
    mock_intent = IntentClassification(
        agent_type=IntentAgentType.ASSESSMENT,
        confidence=0.4,
        needs_clarification=True,
        candidates=[IntentAgentType.ASSESSMENT, IntentAgentType.CARE_FLOW],
        reasoning="跨域无法判断",
    )
    with patch("backend.core.intent.classify_intent", AsyncMock(return_value=mock_intent)):
        orchestrator = get_orchestrator()
        request = AgentRequest(
            staff_id="staff_test_001",
            session_id="clarify-test-001",
            query="评估照护流程",
            mode="single",
            agent_type=None,   # 触发意图识别
        )
        result = await orchestrator.execute(request)

    ok = (
        result.get("mode") == ExecutionMode.CLARIFY.value
        and "candidates" in result
        and len(result["candidates"]) == 2
        and "message" in result
    )
    status = "✅" if ok else "❌"
    print(f"\n  {status} [execute 返回 clarify 模式]")
    print(f"     mode={result.get('mode')}")
    print(f"     candidates={result.get('candidates')}")
    print(f"     message={result.get('message', '')[:80]}")
    return ok


async def test_orchestrator_execute_normal_route():
    """测试 7：orchestrator.execute 正常路由（mock 意图识别返回 SOP_QA，跳过 Agent 执行）。

    这里只验证路由结果正确，不真正调 Agent（避免依赖 Milvus 等）。
    我们 mock _run_single_agent 来确认 agent_type 被正确传递。
    """
    print("\n" + "=" * 60)
    print("  测试 7：orchestrator.execute 正常路由")
    print("=" * 60)

    mock_intent = IntentClassification(
        agent_type=IntentAgentType.SOP_QA,
        confidence=0.95,
        needs_clarification=False,
        candidates=[],
        reasoning="明确问护理操作",
    )
    captured_agent_type = []

    async def _mock_run_single(_self, _req, agent_type):
        captured_agent_type.append(agent_type)
        return {"mode": "single", "agent_type": agent_type, "mock": True}

    with patch("backend.core.intent.classify_intent", AsyncMock(return_value=mock_intent)), \
         patch.object(type(get_orchestrator()), "_run_single_agent", _mock_run_single):
        orchestrator = get_orchestrator()
        request = AgentRequest(
            staff_id="staff_test_001",
            session_id="route-test-001",
            query="鼻饲管怎么维护",
            mode="single",
            agent_type=None,
        )
        result = await orchestrator.execute(request)

    ok = (
        captured_agent_type == ["sop_qa"]
        and result.get("agent_type") == "sop_qa"
    )
    status = "✅" if ok else "❌"
    print(f"\n  {status} [execute 正常路由到 sop_qa]")
    print(f"     routed_agent_type={captured_agent_type}")
    print(f"     result.agent_type={result.get('agent_type')}")
    return ok


# ──────────────────────────────────────────────────────────────
# 主入口
# ──────────────────────────────────────────────────────────────

async def main():
    print("=" * 60)
    print("  意图识别升级测试：LLM 驱动 + 三层 fallback + clarify")
    print("=" * 60)

    results = []
    results.append(("测试 1 - 4 Agent 直达", await test_direct_cases()))
    results.append(("测试 2 - 跨域冲突样本", await test_conflict_cases()))
    results.append(("测试 3 - 闲聊兜底", await test_chitchat_cases()))
    results.append(("测试 4 - LLM 失败 fallback", await test_llm_failure_fallback()))
    results.append(("测试 5 - needs_clarification 触发", await test_clarification_trigger()))
    results.append(("测试 6 - execute → clarify 分支", await test_orchestrator_execute_clarify()))
    results.append(("测试 7 - execute 正常路由", await test_orchestrator_execute_normal_route()))

    # ── 汇总 ──
    print("\n" + "=" * 60)
    print("  汇总")
    print("=" * 60)
    passed = sum(1 for _, ok in results if ok)
    for name, ok in results:
        status = "✅" if ok else "❌"
        print(f"  {status} {name}")
    print(f"\n  总计：{passed}/{len(results)} 通过")

    if passed == len(results):
        print("\n  🎉 所有测试通过")
    else:
        print(f"\n  ⚠️ 有 {len(results) - passed} 项未通过")


if __name__ == "__main__":
    asyncio.run(main())
