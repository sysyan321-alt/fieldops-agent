# backend/agents/assessment/state.py
# 评估 Agent 的状态定义与结构化数据模型

from typing import Annotated, Optional
from typing_extensions import TypedDict
from langgraph.graph.message import add_messages      # 消息列表的「追加」合并器
from langchain_core.messages import BaseMessage
from pydantic import BaseModel, Field


# ──────────────────────────────────────────────────────────────
# 一、结构化提取结果（LLM 结构化输出的目标 Schema）
# ──────────────────────────────────────────────────────────────

class SiteBasicInfo(BaseModel):
    """点位基本信息（从点位评估表 PDF 中提取）。"""
    name:        str = Field(description="点位名称")
    built_year:  int = Field(description="建成年份")
    site_type:   str = Field(description="点位类型：配电间/机房/仓库/公共区域/设备间")
    location:    str = Field(default="", description="位置，如「3号楼B1」")
    risk_tier:   str = Field(default="", description="当前风险等级：三级/二级/一级")
    photo_path:  str = Field(default="", description="照片路径（可选）")


# ──────────────────────────────────────────────────────────────
# 二、五维度评审结果
# ──────────────────────────────────────────────────────────────

class AssessmentDimension(BaseModel):
    """单个评估维度结果。"""
    dimension:    str       = Field(default="", description="维度名称（代码层覆盖，LLM 可留空）")
    score:        int       = Field(description="得分 0-100")
    weight:       float     = Field(default=0.0, description="权重（代码层覆盖）")
    issues:       list[str] = Field(default_factory=list, description="该维度问题列表")
    suggestions:  list[str] = Field(default_factory=list, description="改进建议列表")
    evidence:     str       = Field(default="", description="评分依据原文片段")


class IssueItem(BaseModel):
    """单条诊断问题。"""
    priority:     str = Field(description="优先级：high / medium / low")
    dimension:     str = Field(description="所属维度")
    description:  str = Field(description="问题描述（1句话）")
    location:     str = Field(description="问题在评估表中的定位")
    suggestion:   str = Field(description="具体照护建议（可操作）")


class IssueList(BaseModel):
    """IssueItem 列表的包装类。
    with_structured_output 要求顶层是「对象」而非「裸列表」。"""
    items: list[IssueItem]


class DiagnosisAndSummary(BaseModel):
    """合并后的一次性输出：逐条问题诊断 + 整体评价。
    用单次 LLM 调用替代原来 diagnose_issues + generate_summary 两次串行调用。"""
    issues: list[IssueItem] = Field(description="逐条问题诊断清单（5-15条）")
    highlights: list[str] = Field(description="2-3 条核心亮点")
    core_improvements: list[str] = Field(description="2-3 条最重要的改进方向")
    overall_comment: str = Field(description="1-2 句综合评语")
    risk_tier_recommendation: str = Field(description="建议护理等级：三级/二级/一级")


class AssessmentSummary(BaseModel):
    """评估整体评价——generate_summary 节点的输出目标。"""
    highlights:               list[str] = Field(description="2-3 条核心亮点")
    core_improvements:        list[str] = Field(description="2-3 条最重要的改进方向")
    overall_comment:          str       = Field(description="1-2 句综合评语")
    risk_tier_recommendation: str      = Field(description="建议护理等级：三级/二级/一级")


# ──────────────────────────────────────────────────────────────
# 三、评估 Agent 完整 State
# ──────────────────────────────────────────────────────────────

class AssessmentState(TypedDict):
    """评估 Agent 完整 State。字段按数据流阶段分组，对应各节点的产出。"""

    # ── 请求上下文 ──
    messages:          Annotated[list[BaseMessage], add_messages]  # 对话消息（追加合并）
    site_id:          str
    tenant_id:         str
    assessment_id:     str        # 评估记录 ID
    pdf_local_path:    str        # 本地临时文件路径

    # ── 解析中间结果 ──
    raw_text:          str        # parse_pdf 产出：PDF 全文
    page_count:        int        # PDF 页数

    # ── 结构化提取结果 ──
    structured:        Optional[dict]   # extract_structured 产出：SiteBasicInfo.model_dump()

    # ── 五维度评审结果 ──
    dimension_scores:  list[dict]     # run_five_dimensions 产出：五维度各一条
    weighted_score:    float          # 加权综合得分 0-100

    # ── 逐条问题诊断 ──
    issues:            list[dict]     # diagnose_issues 产出：IssueItem 列表

    # ── 整体评价 ──
    summary:           Optional[dict]  # generate_summary 产出：AssessmentSummary.model_dump()

    # ── 降级标记 ──
    fallback_used:     bool
    structured_output: Optional[dict]
