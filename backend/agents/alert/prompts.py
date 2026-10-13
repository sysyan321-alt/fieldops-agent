# backend/agents/alert/prompts.py
# 告警研判 Agent 的所有 Prompt 模板

# ──────────────────────────────────────────────────────────────
# 系统提示：定义 LLM 的「人设」
# ──────────────────────────────────────────────────────────────

SYSTEM_PROMPT = """你是一位养老机构告警研判 AI 助手，专门辅助护理团队处理 IoT 设备触发的健康告警。
你的研判严格、专业、可执行，基于老年人照护标准和应急预案，
给出的风险等级和处置措施必须具体、可操作，不给出模糊的套话。"""


# ──────────────────────────────────────────────────────────────
# 风险评估   占位符：{alert_type} {alert_timestamp} {device_profile}
#            {evidence_quality} {evidence_gaps} {device_readings}
#            {service_records} {health_indicators} {assessment_snapshot}
# 用于：assess_risk 节点
# ──────────────────────────────────────────────────────────────

RISK_ASSESS_PROMPT = """请基于以下四轨数据，评估本次告警的风险等级和处置建议。

【告警信息】
告警类型：{alert_type}
触发时间：{alert_timestamp}
{device_profile}

【证据完整性】
完整性：{evidence_quality}（complete / partial / insufficient）
缺失的证据：{evidence_gaps}

【第一轨：设备读数】
{device_readings}

【第二轨：近期照护记录】
{service_records}

【第三轨：健康指标】
{health_indicators}

【第四轨：老人最近一次能力评估（没有则显示"（暂无评估记录）"）】
{assessment_snapshot}

【评估要求】
1. risk_level：根据数据综合判断风险等级
   - high：危及生命或需紧急处置（如：心率骤停/严重跌倒/长时间离床）
   - medium：需及时关注但非紧急（如：心率偏高/短暂离床/体温偏高）
   - low：误报可能大或无需紧急处置
2. risk_score：0-100 分（越高风险越大）
3. reasoning：2-3 句话说明评估依据，并**指明结论主要由哪条证据支撑**
4. recommended_actions：2-5 条具体可操作的处置措施
   - 该字段必须输出，不能省略、不能是空数组；
   - 每条都要是护理员照着能做出来的动作（谁去做、做什么），不要写"加强观察"这类无法执行的空话
5. confidence：评估置信度 [0,1]

【证据纪律——这几条优先于上面的等级判断】
6. 缺失的证据就是缺失，**不得推测、不得脑补**：没有的数据一律按"未知"处理，
   不要在 reasoning 里写出数据里没有的数值。
7. 证据完整性为 insufficient 时：**不得判 low**（不能因为"查不到异常"就下"没事"的结论，
   只能判 medium 及以上并说明需要人工核实），且 confidence 不得超过 0.5。
8. 证据可信度 ≤ 2 的设备（如人流测温）：单凭它不足以定性，结论必须要求现场复核。
9. 只有单一来源证据时，reasoning 里要写明"仅单源证据"，不要写成已确认。"""


# ──────────────────────────────────────────────────────────────
# 派发工单   占位符：{risk_level} {recommended_actions} {supervisor_decision}
# 用于：dispatch_order 节点
# ──────────────────────────────────────────────────────────────

DISPATCH_ORDER_PROMPT = """请基于风险评估结果和站长确认，生成派发工单内容。

【风险评估】
风险等级：{risk_level}
建议措施：{recommended_actions}

【站长确认】
{supervisor_decision}

【工单要求】
1. 措施明确：每条措施必须是可执行的护理操作
2. 责任到人：标注执行角色（护理员/护士/站长/医生）
3. 时限要求：标注紧急程度（立即/15分钟内/30分钟内/本班次内）
4. 注意事项：标出需要特别观察的指标

请以结构化文本输出工单内容。"""
