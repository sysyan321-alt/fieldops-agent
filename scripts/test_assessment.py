# scripts/test_assessment.py
# 评估 Agent 端到端测试（并行评审范式）
# 完整跑通：parse_pdf → extract_structured → run_five_dimensions（并行）→ diagnose_issues → generate_summary → save_results
#
# 用法：cd <项目根目录> && PYTHONPATH=. python scripts/test_assessment.py

import asyncio
import os
import tempfile
from unittest.mock import patch

import fitz  # PyMuPDF（节点1 parse_pdf 也用它）

from backend.agents.assessment.graph import build_assessment_graph
from backend.agents.assessment import nodes as assess_nodes
from backend.core.logger import configure_logging, get_logger

configure_logging()
logger = get_logger(__name__)


def _mock_sync_parse_pdf(pdf_path: str) -> dict:
    """mock 版 PDF 解析：直接返回完整中文评估表文本。
    PyMuPDF 默认字体（Helvetica）不支持中文，insert_text 写入的中文会变成 '·'。
    真实场景 PDF 是扫描件或含中文字体，PyMuPDF 能正常提取。
    这里 mock 返回完整文本，以验证后续 LLM 节点的真实能力。
    """
    return {"raw_text": ASSESSMENT_TEXT, "page_count": 2}


# ── 测试用评估表全文（模拟一份真实的老人能力评估表）──────────────
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


def create_test_pdf(pdf_path: str) -> int:
    """用 PyMuPDF 创建测试用评估表 PDF，返回页数。"""
    doc = fitz.open()
    page = doc.new_page()
    lines = ASSESSMENT_TEXT.strip().split("\n")
    per_page = 40
    for i, line in enumerate(lines):
        if i > 0 and i % per_page == 0:
            page = doc.new_page()
        y = 50 + (i % per_page) * 18
        page.insert_text((72, y), line, fontsize=11)
    doc.save(pdf_path)
    page_count = len(doc)
    doc.close()
    return page_count


async def run_assessment_full_flow():
    """完整跑通评估 Agent 的 6 节点链路。"""
    print("\n" + "=" * 60)
    print("  评估 Agent 端到端测试（并行评审范式）")
    print("=" * 60)

    # ── 1. 创建测试 PDF ──
    tmp_dir = tempfile.mkdtemp(prefix="assessment_test_")
    pdf_path = os.path.join(tmp_dir, "test_assessment.pdf")
    page_count = create_test_pdf(pdf_path)
    print(f"\n[1] 测试 PDF 已生成：{pdf_path}（{page_count} 页）")

    # ── 2. 构造初始 State ──
    initial_state = {
        "messages": [],
        "elder_id":         "e001",
        "tenant_id":        "tenant_default",
        "assessment_id":    "assess-test-001",
        "pdf_local_path":   pdf_path,
        "raw_text":         "",
        "page_count":       0,
        "structured":       None,
        "dimension_scores": [],
        "weighted_score":   0.0,
        "issues":           [],
        "summary":          None,
        "fallback_used":    False,
        "structured_output": None,
    }
    print(f"\n[2] 初始 State 构造完成，开始运行 6 节点链路...")

    # ── 3. 运行图（无 checkpointer，一次性任务）──
    # 用 mock 替换 _sync_parse_pdf，返回完整中文文本（绕开 PyMuPDF 中文字体限制）
    graph = build_assessment_graph()
    print(f"\n[3] 节点链路：")
    print(f"    parse_pdf（mock 返回完整中文）→ extract_structured → run_five_dimensions（并行）")
    print(f"    → diagnose_issues → generate_summary → save_results")

    with patch.object(assess_nodes, "_sync_parse_pdf", side_effect=_mock_sync_parse_pdf):
        result = await graph.ainvoke(initial_state)

    # ── 4. 打印各节点产出 ──
    print(f"\n[4] ① parse_pdf 产出：")
    print(f"    page_count:     {result.get('page_count')}")
    print(f"    raw_text 长度:  {len(result.get('raw_text', ''))} 字符")
    print(f"    raw_text 前 200 字：")
    print(f"    {result.get('raw_text', '')[:200]}")

    print(f"\n[5] ② extract_structured 产出（老人基本信息）：")
    s = result.get("structured") or {}
    print(f"    name:       {s.get('name')}")
    print(f"    age:        {s.get('age')}")
    print(f"    gender:     {s.get('gender')}")
    print(f"    room_no:     {s.get('room_no')}")
    print(f"    care_level: {s.get('care_level')}")

    print(f"\n[6] ③ run_five_dimensions 产出（五维度并行评审）：")
    dim_scores = result.get("dimension_scores", [])
    for d in dim_scores:
        print(f"    - {d.get('dimension',''):>14} (权重{int(d.get('weight',0)*100)}%): "
              f"{d.get('score')} 分 | issues: {len(d.get('issues', []))} 条")
    print(f"    加权综合得分: {result.get('weighted_score')} / 100")

    print(f"\n[7] ④ diagnose_issues 产出（逐条问题诊断）：")
    issues = result.get("issues", [])
    print(f"    共 {len(issues)} 条问题")
    for i, issue in enumerate(issues[:8], 1):
        print(f"    {i}. [{issue.get('priority','low').upper():>6}] "
              f"{issue.get('dimension','')} - {issue.get('description','')}")

    print(f"\n[8] ⑤ generate_summary 产出（整体评价）：")
    summary = result.get("summary") or {}
    print(f"    综合评语:      {summary.get('overall_comment','')}")
    print(f"    护理等级建议:  {summary.get('care_level_recommendation','')}")
    print(f"    亮点:")
    for h in summary.get("highlights", [])[:3]:
        print(f"      - {h}")
    print(f"    核心改进方向:")
    for c in summary.get("core_improvements", [])[:3]:
        print(f"      - {c}")

    print(f"\n[9] ⑥ save_results 产出（structured_output）：")
    so = result.get("structured_output") or {}
    print(f"    assessment_id:  {so.get('assessment_id')}")
    print(f"    elder_id:        {so.get('elder_id')}")
    print(f"    weighted_score:  {so.get('weighted_score')}")
    print(f"    issues_count:    {len(so.get('issues', []))}")
    print(f"    fallback_used:   {result.get('fallback_used')}")

    print("\n" + "=" * 60)
    print("  ✅ 评估 Agent 6 节点链路跑通！")
    print("=" * 60)

    # 清理临时文件
    try:
        os.remove(pdf_path)
        os.rmdir(tmp_dir)
    except OSError:
        pass

    return result


if __name__ == "__main__":
    asyncio.run(run_assessment_full_flow())
