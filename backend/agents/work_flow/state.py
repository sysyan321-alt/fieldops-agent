# backend/agents/work_flow/state.py
# 照护流程 Agent 的状态定义

from typing import Annotated, Optional
from typing_extensions import TypedDict
from langgraph.graph.message import add_messages
from langchain_core.messages import BaseMessage
from pydantic import BaseModel, Field
from enum import Enum


class WorkStage(str, Enum):
    """照护流程的四个阶段（异常处理改为日常照护内的子模式，不再作为独立阶段）。

    设计取舍：异常是事件不是阶段——晨间护理发现压红、鼻饲时发现胃液异常
    本就是日常照护动作中发现的，把它做成独立阶段会导致：①老人没异常时空转；
    ②日常上下文被打散；③报告里"异常处理阶段得分"语义错位。
    正确做法是日常照护内用 daily_mode 字段切换 normal/abnormal 模式。
    """
    ADMISSION_ASSESSMENT = "admission_assessment"  # 入住评估
    DAILY_CARE           = "daily_care"             # 日常照护（内含异常子模式）
    PERIODIC_REVIEW      = "periodic_review"         # 阶段复评
    DISCHARGE_ASSESSMENT = "discharge_assessment"    # 出院评估
    FINISHED             = "finished"               # 终态：触发报告生成


class DailyCareMode(str, Enum):
    """日常照护阶段内的子模式：常规模式 / 异常模式。

    normal → abnormal：关键词触发（晨护发现压红、鼻饲胃液异常等）
    abnormal → normal：用户说"处理完/异常已处理"或达到异常子模式轮数上限
    """
    NORMAL   = "normal"
    ABNORMAL = "abnormal"


class ResponseQuality(str, Enum):
    """护理员单次回答的质量标签（驱动后续照护动作）。"""
    GOOD       = "good"        # 良好：照护操作规范、记录完整
    ADEQUATE   = "adequate"    # 基本及格：操作基本到位但缺细节
    POOR       = "poor"        # 较差：操作不规范或有遗漏
    NO_RESPONSE = "no_response"  # 未作答：未回复或为空


class DimensionEval(BaseModel):
    """单个维度的评估结果"""
    dimension:    str       = Field(description="评估维度名称")
    score:        int       = Field(description="得分 0-100")
    comment:      str       = Field(description="1-2句评语")
    highlights:   list[str] = Field(default_factory=list, description="亮点列举")
    weaknesses:   list[str] = Field(default_factory=list, description="薄弱点列举")


class WorkReport(BaseModel):
    """照护流程评估报告"""
    dimensions:         list[DimensionEval]
    overall_score:      int       = Field(description="综合得分 0-100（各维度加权平均）")
    strengths:          list[str] = Field(description="2-3条核心优势")
    improvements:       list[str] = Field(description="2-3条重点提升方向")
    service_suggestions:   list[str] = Field(description="2-3条照护建议")
    next_review_date:   str       = Field(description="下次复评建议日期")


class WorkFlowState(TypedDict):
    """照护流程 Agent 完整 State（跨轮由 checkpointer 持久化，默认 AsyncPostgresSaver 落 PostgreSQL）"""

    # ── 请求上下文 ──
    messages:           Annotated[list[BaseMessage], add_messages]
    site_id:           str
    tenant_id:          str
    session_id:         str
    service_plan_id:       str           # 照护计划 ID

    # ── 阶段控制 ──
    current_stage:        str           # 当前阶段，WorkStage 枚举值
    stage_turn_count:     int           # 当前阶段已进行的轮数
    total_turn_count:     int           # 总轮数（跨阶段累计）
    max_turns:            int           # 轮数上限，默认 40
    daily_mode:      str           # 日常照护子模式：normal / abnormal
    abnormal_turn_count:  int           # 异常子模式内的轮数（仅 abnormal 累计）
    discharge_order:      Optional[dict]  # 退住工单（SaaS 注入，None=续住；阶段复评时据此决定出院评估 or 回日常照护）

    # ── 照护计划与历史 ──
    service_plan:          Optional[dict]  # 照护计划（来自 Provider：业务服务项 + 评估结论）
    service_plan_missing:  bool            # 计划缺失（取不到数据时为 True，流程不推进）
    plan_source:        str             # 计划来源：pipeline/service_items/ai_assessment/merged/missing
    service_history:       list[dict]      # 照护历史记录（真实记录，取不到就是空）
    today_plan:         list[dict]      # 当天照护计划（含执行状态，来自 Provider）
    plan_progress:      dict            # 当天进度 {total, done, missed, pending}

    # ── 回答质量追踪 ──
    last_response_quality: str         # 上一条回答的质量标签
    followup_count:      int           # 当前问题已追问次数

    # ── 记忆管理 ──
    existing_summary:    Optional[str]  # 历史对话摘要
    summary_message_count: int          # 该摘要已覆盖的消息条数（判断何时再压缩）
    should_summarize:    bool           # 本轮是否需要触发摘要压缩

    # ── 评估结果 ──
    report:              Optional[dict]  # WorkReport.model_dump()

    # ── 降级标记 ──
    fallback_used:       bool
    structured_output:   Optional[dict]
