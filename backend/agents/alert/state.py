# backend/agents/alert/state.py
# 告警研判 Agent 的状态定义与结构化数据模型

from typing import Annotated, Optional
from typing_extensions import TypedDict
from langgraph.graph.message import add_messages
from langchain_core.messages import BaseMessage
from pydantic import BaseModel, Field


# ──────────────────────────────────────────────────────────────
# 一、四轨数据采集的结构化模型（第四轨为风险评估快照，见 AlertState.assessment_snapshot）
# ──────────────────────────────────────────────────────────────

class DeviceReading(BaseModel):
    """单条设备读数。"""
    device_id:     str = Field(description="设备 ID")
    device_type:   str = Field(description="设备类型：配电监测/视频行为分析/水位监测/环境监测站/门禁")
    reading_value: str = Field(description="读数值")
    reading_unit:  str = Field(description="读数单位")
    timestamp:     str = Field(description="采集时间")
    is_abnormal:   bool = Field(default=False, description="是否异常")


class ServiceRecord(BaseModel):
    """单条服务记录。"""
    record_id:    str = Field(description="记录 ID")
    site_id:     str = Field(description="点位 ID")
    service_type:    str = Field(description="服务类型：巡检/设备检查/环境检查/消防设施检查/用电检查")
    content:      str = Field(description="服务内容")
    worker_id: str = Field(description="作业人员 ID")
    timestamp:    str = Field(description="记录时间")


class HealthIndicator(BaseModel):
    """单条健康指标。"""
    indicator_name:  str = Field(description="指标名称，如设备温度/环境湿度/电流/电压")
    value:           str = Field(description="指标值")
    unit:            str = Field(description="单位")
    reference_range: str = Field(description="参考范围")
    status:          str = Field(description="状态：normal/high/low/abnormal")
    timestamp:       str = Field(description="采集时间")


# ──────────────────────────────────────────────────────────────
# 二、风险评估结果
# ──────────────────────────────────────────────────────────────

class RiskAssessment(BaseModel):
    """告警风险评估结果（LLM 结构化输出）。"""
    risk_level:         str = Field(description="风险等级：high/medium/low")
    risk_score:         int = Field(description="风险分数 0-100")
    reasoning:          str = Field(description="风险评估依据（2-3句）")
    recommended_actions: list[str] = Field(
        default_factory=list,
        description="建议处置措施列表，2-5 条可执行动作，不得为空数组（节点侧还有空值兜底）",
    )
    # 注意：这里的 default_factory 会让该字段不进 tool schema 的 required，
    # 模型可能省略它（走 function_calling 时属合法输出）。因此调用方不能假设它非空，
    # 必须在节点里兜底——见 nodes.py 的 ensure_actions。
    confidence:         float = Field(description="评估置信度 [0,1]")


# ──────────────────────────────────────────────────────────────
# 三、值班主管确认决策
# ──────────────────────────────────────────────────────────────

class SupervisorDecision(BaseModel):
    """值班主管确认决策（interrupt 恢复时传入）。"""
    action:           str = Field(description="approve / modify / dismiss")
    modified_actions: list[dict] = Field(default_factory=list, description="修改后的处置措施")
    supervisor_id:    str = Field(description="值班主管 ID")
    comment:          str = Field(default="", description="值班主管备注")


# ──────────────────────────────────────────────────────────────
# 四、告警研判 Agent 完整 State
# ──────────────────────────────────────────────────────────────

class AlertState(TypedDict):
    """告警研判 Agent 完整 State。"""

    # ── 请求上下文 ──
    messages:          Annotated[list[BaseMessage], add_messages]
    site_id:          str
    tenant_id:         str
    alert_id:          str         # 告警记录 ID
    session_id:        str
    device_id:         str         # 触发告警的设备 ID
    alert_type:        str         # heart_rate_abnormal / off_bed_timeout / fall_detected / room_temp_abnormal
    alert_timestamp:   str         # 告警触发时间
    source:            str         # 来源：local=本服务自触发 / saas=业务系统下发报警记录
    external_alert_id:         str         # SaaS 侧报警记录 ID（回写处置结果时用）
    alert_location:    str         # 报警位置（对齐 SaaS 报警记录 的"报警位置"）
    device_type_declared: str      # 报警自带的设备类型声明（SaaS 报警记录 的"设备类型"）

    # ── 设备谱系（由 resolve_device_profile 解析，见 backend/core/device_types.py）──
    device_profile:    Optional[dict]  # {type_code, type_name, trust_level, location, source}

    # ── 四轨数据采集结果 ──
    device_readings:     list[dict]        # 第一轨：设备读数列表
    service_records:        list[dict]        # 第二轨：近期服务记录列表
    health_indicators:   list[dict]        # 第三轨：健康指标列表
    assessment_snapshot: Optional[dict]    # 第四轨：点位最近一次风险评估（含五维得分与问题清单）
    evidence_gaps:       list[str]         # 缺失的证据轨（显式声明，不允许脑补）
    evidence_quality:    str               # complete / partial / insufficient

    # ── 风险评估 ──
    risk_assessment:   Optional[dict]  # RiskAssessment.model_dump()

    # ── Human-in-the-Loop ──
    supervisor_notified: bool
    supervisor_decision: Optional[dict]  # SupervisorDecision.model_dump()

    # ── 最终结果 ──
    dispatch_order:    Optional[dict]  # 派发工单内容
    published:         bool

    # ── 降级标记 ──
    fallback_used:     bool
    structured_output: Optional[dict]
