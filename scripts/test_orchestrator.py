# scripts/test_orchestrator.py
# Orchestrator Pipeline 端到端串联测试
# 验证 service_workflow 流水线：assessment → work_flow（上下文传递）
#
# 用法：cd <项目根目录> && PYTHONPATH=. python scripts/test_orchestrator.py

import asyncio
import os
import tempfile
from unittest.mock import patch

from backend.core.orchestrator import get_orchestrator, AgentRequest
from backend.agents.assessment import nodes as assess_nodes
from backend.core.logger import configure_logging, get_logger

configure_logging()
logger = get_logger(__name__)


# ── mock PDF 解析：绕开 PyMuPDF 中文字体限制，直接返回完整评估表文本 ──
ASSESSMENT_TEXT = """老年人能力评估表

【基本信息】
姓名：张建国    性别：男    年龄：78岁
房间号：301      护理等级：介助
评估日期：2026-09-18

【ADL 日常生活活动能力评估】
1. 进食：需他人协助切分食物，能自行进食。评分：2/3（中度依赖）
2. 穿衣：能自行穿衣，但扣纽扣需协助。评分：2/3
3. 如厕：能自行如厕，夜间需提醒。
4. 移动：可独立行走，步态稍不稳。
5. 洗澡：需他人协助洗浴，防止跌倒。

【认知功能评估（MMSE）】
定向力：能正确说出年月日，地点定向良好。
记忆力：近事记忆减退，3分钟后回忆遗忘。
计算力：100-7 连续减法可完成 2 次。
判断力：对常见问题判断正确。
语言能力：表达流畅，偶有找词困难。
MMSE 得分：22/30（轻度认知障碍）

【营养状态评估（MNA）】
BMI：21.5（正常偏低）
近 3 个月体重下降 2kg。
每日进食量：主食约 150g，蛋白质摄入不足。
血清白蛋白：35g/L（正常下限）。
MNA 得分：9/14（营养不良风险）

【跌倒风险评估（Morse）】
跌倒史：近 1 年跌倒 1 次，滑倒致髋部挫伤。
步态：步态稍不稳，起步犹豫。
辅助器具：无。
用药：服用降压药 1 种，安眠药偶尔使用。
视力：双眼白内障术后，视力 0.6。
Morse 得分：45（跌倒风险中等）

【社交参与评估】
集体活动：每周参加 1 次手工活动。
人际交流：与室友交流正常，较少主动与其他老人交谈。
情绪状态：情绪平稳，偶有低落。
抑郁筛查 GDS：6/15（正常范围）

【综合评估意见】
老人轻度认知减退，跌倒风险中等，营养风险边缘，日常基本自理。
建议：加强认知训练，预防跌倒，营养干预。"""


def _mock_sync_parse_pdf(pdf_path: str) -> dict:
    """mock 版 PDF 解析，返回完整中文文本。"""
    return {"raw_text": ASSESSMENT_TEXT, "page_count": 2}


def _create_test_pdf(pdf_path: str) -> int:
    """创建测试 PDF 占位文件（内容不重要，parse_pdf 会被 mock）。"""
    import fitz
    doc = fitz.open()
    doc.new_page()
    doc.save(pdf_path)
    page_count = len(doc)
    doc.close()
    return page_count


# ──────────────────────────────────────────────────────────────
# 主测试：Pipeline service_workflow 端到端串联
# ──────────────────────────────────────────────────────────────

async def run_pipeline_service_workflow():
    """验证 Pipeline service_workflow：assessment → work_flow。

    核心断言：
        1. 两个 Agent 都跑通，无异常
        2. assessment 产出 structured_output（含 weighted_score / summary）
        3. 上下文从 assessment 传递到 work_flow（service_plan 不再是 mock）
        4. work_flow 的 service_plan 字段含 weighted_score / risk_tier（来自评估）
        5. work_flow AI 响应非空
        6. 最终 aggregated.structured_output 包含两个 Agent 的输出
    """
    print("\n" + "=" * 60)
    print("  Orchestrator Pipeline 端到端串联测试")
    print("  Pipeline: service_workflow = [assessment, work_flow]")
    print("=" * 60)

    # ── 1. 准备测试 PDF ──
    tmp_dir = tempfile.mkdtemp(prefix="orchestrator_test_")
    pdf_path = os.path.join(tmp_dir, "test_assessment.pdf")
    page_count = _create_test_pdf(pdf_path)
    print(f"\n[1] 测试 PDF 已生成：{pdf_path}（{page_count} 页，parse_pdf 将被 mock）")

    # ── 2. 构造 AgentRequest（Pipeline 模式）──
    session_id = "pipeline-work-001"
    request = AgentRequest(
        staff_id="staff_test_001",
        site_id="e001",
        tenant_id="tenant_default",
        session_id=session_id,
        mode="pipeline",
        pipeline="service_workflow",
        context={
            "assessment_id": "assess-pipeline-001",
            "pdf_local_path": pdf_path,
            "service_plan_id": f"plan-{session_id}",
        },
    )
    print(f"\n[2] AgentRequest 构造完成")
    print(f"    staff_id={request.staff_id}, site_id={request.site_id}")
    print(f"    session_id={session_id}, thread_id={request.thread_id}")
    print(f"    mode=pipeline, pipeline={request.pipeline}")

    # ── 3. 调用 Orchestrator（mock parse_pdf）──
    orchestrator = get_orchestrator()
    print(f"\n[3] 调用 orchestrator.execute(request)...（mock parse_pdf）")

    with patch.object(assess_nodes, "_sync_parse_pdf", side_effect=_mock_sync_parse_pdf):
        result = await orchestrator.execute(request)

    # ── 4. 打印结果 ──
    print(f"\n[4] Pipeline 执行完成，结果汇总：")
    print(f"    mode:        {result.get('mode')}")
    print(f"    pipeline:    {result.get('pipeline')}")
    print(f"    site_id:    {result.get('site_id')}")
    print(f"    session_id:  {result.get('session_id')}")

    # ── 5. 验证 step 1: assessment ──
    results = result.get("results", {})
    assessment_result = results.get("assessment", {})
    print(f"\n[5] ── Step 1: assessment ──")
    print(f"    fallback_used:     {assessment_result.get('fallback_used')}")
    assess_so = assessment_result.get("structured_output") or {}
    print(f"    assessment_id:     {assess_so.get('assessment_id')}")
    print(f"    weighted_score:   {assess_so.get('weighted_score')}")
    summary = assess_so.get("summary") or {}
    print(f"    risk_tier_recom:  {summary.get('risk_tier_recommendation')}")
    print(f"    overall_comment:   {(summary.get('overall_comment') or '')[:60]}")
    print(f"    issues_count:      {len(assess_so.get('issues', []))}")

    assert assess_so.get("weighted_score"), "assessment 应产出 weighted_score"
    assert summary.get("risk_tier_recommendation"), "assessment 应产出护理等级建议"

    # ── 6. 验证 step 2: work_flow（上下文已传递）──
    work_flow_result = results.get("work_flow", {})
    print(f"\n[6] ── Step 2: work_flow（验证上游上下文已传递）──")
    service_plan_in_state = work_flow_result.get("service_plan") or {}
    print(f"    service_plan.plan_id:        {service_plan_in_state.get('plan_id')}")
    print(f"    service_plan.risk_tier:     {service_plan_in_state.get('risk_tier')}")
    print(f"    service_plan.weighted_score: {service_plan_in_state.get('weighted_score')}")
    print(f"    service_plan.daily_tasks:    {len(service_plan_in_state.get('daily_tasks', []))} 条")
    print(f"    service_plan.special_notes:  {(service_plan_in_state.get('special_notes') or '')[:60]}")

    # 关键断言：service_plan 不应是 mock 数据，应携带 assessment 的 weighted_score
    assert service_plan_in_state.get("weighted_score") is not None, \
        "work_flow 的 service_plan 应携带上游 assessment 的 weighted_score（验证上下文传递）"
    assert service_plan_in_state.get("risk_tier") == summary.get("risk_tier_recommendation"), \
        "service_plan.risk_tier 应等于 assessment 的 risk_tier_recommendation"
    assert service_plan_in_state.get("plan_id") == f"plan-{assess_so.get('assessment_id')}", \
        "service_plan.plan_id 应从 assessment_id 派生"

    # ── 7. work_flow AI 响应 ──
    messages = work_flow_result.get("messages", [])
    ai_msg = ""
    if messages:
        last = messages[-1]
        if hasattr(last, "content"):
            ai_msg = last.content if isinstance(last.content, str) else str(last.content)
    print(f"\n[7] ── work_flow AI 响应 ──")
    print(f"    current_stage:          {work_flow_result.get('current_stage')}")
    print(f"    stage_turn_count:       {work_flow_result.get('stage_turn_count')}")
    print(f"    last_response_quality: {work_flow_result.get('last_response_quality')}")
    print(f"    AI 响应长度: {len(ai_msg)} 字符")
    print(f"    AI 响应前 300 字：")
    print(f"    {ai_msg[:300]}")

    assert len(ai_msg) > 50, "work_flow 应产出非空 AI 响应"

    # ── 8. 验证 aggregated.structured_output ──
    aggregated_so = result.get("structured_output", {})
    print(f"\n[8] ── aggregated.structured_output ──")
    print(f"    包含的 Agent: {list(aggregated_so.keys())}")
    print(f"    assessment.weighted_score: {(aggregated_so.get('assessment') or {}).get('weighted_score')}")
    work_flow_so = aggregated_so.get("work_flow") or {}
    print(f"    work_flow.service_plan_id:    {work_flow_so.get('service_plan_id')}")
    print(f"    work_flow.site_id:       {work_flow_so.get('site_id')}")

    assert "assessment" in aggregated_so, "aggregated 应包含 assessment"
    assert "work_flow" in aggregated_so, "aggregated 应包含 work_flow"

    # ── 9. 清理临时文件 ──
    try:
        os.remove(pdf_path)
        os.rmdir(tmp_dir)
    except OSError:
        pass

    print("\n" + "=" * 60)
    print("  ✅ Orchestrator Pipeline service_workflow 端到端串联跑通！")
    print("  - assessment 5 维度并行评审 → structured_output")
    print("  - 上下文转换：assessment_result → service_plan（含 weighted_score/risk_tier）")
    print("  - work_flow 保留上游 service_plan → AI 响应基于评估结论")
    print("  - aggregated 汇总两个 Agent 输出")
    print("=" * 60)

    return result


# ──────────────────────────────────────────────────────────────
# 补充测试：单 Agent 模式（确认改造未破坏单 Agent 路径）
# ──────────────────────────────────────────────────────────────

async def run_single_agent_smoke():
    """单 Agent 模式 smoke test：直接调用 work_flow，验证 mock service_plan 兜底正常。"""
    print("\n" + "=" * 60)
    print("  补充测试：单 Agent 模式（work_flow mock 兜底）")
    print("=" * 60)

    session_id = "single-workflow-smoke"
    request = AgentRequest(
        staff_id="staff_test_001",
        site_id="e001",
        tenant_id="tenant_default",
        session_id=session_id,
        agent_type="work_flow",
        mode="single",
        query="老人今天精神不错，晨间护理已完成。",
        context={
            "service_plan_id": f"plan-{session_id}",
            "current_stage": "admission_assessment",
        },
    )

    orchestrator = get_orchestrator()
    print(f"\n[1] 调用 orchestrator.execute(request)（mode=single, agent=work_flow）")

    result = await orchestrator.execute(request)

    service_plan = result.get("service_plan") or {}
    print(f"\n[2] service_plan 兜底验证（应走 mock 分支）：")
    print(f"    plan_id:    {service_plan.get('plan_id')}")
    print(f"    risk_tier: {service_plan.get('risk_tier')}")
    print(f"    daily_tasks: {len(service_plan.get('daily_tasks', []))} 条")
    print(f"    weighted_score: {service_plan.get('weighted_score')}（应缺失，mock 无此字段）")

    # mock service_plan 不应含 weighted_score
    assert "weighted_score" not in service_plan, \
        "单 Agent 模式下 service_plan 应是 mock，不含 weighted_score"
    assert service_plan.get("risk_tier") == "介助", \
        "mock service_plan 的 risk_tier 应是默认的 '介助'"

    messages = result.get("messages", [])
    ai_msg = ""
    if messages:
        last = messages[-1]
        if hasattr(last, "content"):
            ai_msg = last.content if isinstance(last.content, str) else str(last.content)
    print(f"\n[3] AI 响应（长度 {len(ai_msg)}）：")
    print(f"    {ai_msg[:200]}")

    print("\n" + "=" * 60)
    print("  ✅ 单 Agent 模式正常（mock 兜底未被破坏）！")
    print("=" * 60)


if __name__ == "__main__":
    asyncio.run(run_pipeline_service_workflow())
    asyncio.run(run_single_agent_smoke())
