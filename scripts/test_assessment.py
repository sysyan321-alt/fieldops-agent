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
ASSESSMENT_TEXT = """点位风险评估表

【基本信息】
点位名称：3号楼B1配电间    点位类型：配电间
位置：3号楼B1              建成年份：2008
风险等级：二级
评估日期：2026-09-18

【结构安全评估】
1. 承重与围护：墙体完好，无渗漏痕迹。
2. 地面：局部起砂，无积水。
3. 门窗：防火门闭门器完好，门缝密封条轻微老化。
4. 屋面：管道穿墙处防水收口完好，无明显渗漏。
5. 标识：设备标识清晰，安全出口指示完好。

【设备状态评估】
配电柜：3 面，2015 年更换，近一年无故障记录。
母排与接点：B 相接头有温升记录，红外测温最高 62℃。
仪表：电流表、电压表读数正常，计量表在检验有效期内。
维保记录：2026-06 完成年度保养，滤网已清洁。

【环境风险评估】
温湿度：柜内温度 48℃，环境湿度 68%。
通风：通风百叶部分被包装箱遮挡。
可燃物：配电间内堆放纸质包装箱 3 箱。
渗漏：无积水，地面干燥。

【作业风险评估】
作业类型：日常巡检为主，偶有停电检修。
高处作业：无。
动火作业：无固定动火点，检修动火需办理许可。
受限空间：无。
个人防护：绝缘手套、绝缘鞋均在检验有效期内。

【合规性评估】
证照：电工作业证 2 人，均在有效期内。
检验：绝缘工具检验有效期至 2027-03。
记录：巡检记录完整，最近一次交接班记录缺签名。
标识：止步高压危险标识清晰。

【综合评估意见】
点位整体状况中等，主要问题为柜内温升偏高、通风被遮挡、可燃物堆放。
风险等级建议：二级。建议清理可燃物、恢复通风、加密温升监测。"""


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
        "site_id":         "s001",
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
    print(f"    built_year:        {s.get('built_year')}")
    print(f"    site_type:     {s.get('site_type')}")
    print(f"    location:     {s.get('location')}")
    print(f"    risk_tier: {s.get('risk_tier')}")

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
    print(f"    护理等级建议:  {summary.get('risk_tier_recommendation','')}")
    print(f"    亮点:")
    for h in summary.get("highlights", [])[:3]:
        print(f"      - {h}")
    print(f"    核心改进方向:")
    for c in summary.get("core_improvements", [])[:3]:
        print(f"      - {c}")

    print(f"\n[9] ⑥ save_results 产出（structured_output）：")
    so = result.get("structured_output") or {}
    print(f"    assessment_id:  {so.get('assessment_id')}")
    print(f"    site_id:        {so.get('site_id')}")
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
