# backend/agents/care_flow/nodes.py
# 照护流程 Agent 的所有节点函数

import json
from datetime import datetime, timedelta

from langchain_core.messages import HumanMessage, AIMessage, SystemMessage, BaseMessage

from backend.agents.care_flow.state import (
    CareFlowState, CareStage, DailyCareMode, ResponseQuality, CareReport,
)
from backend.agents.care_flow.prompts import (
    SYSTEM_PROMPT, ADMISSION_PROMPT, DAILY_CARE_PROMPT, ABNORMAL_PROMPT,
    REVIEW_PROMPT, DISCHARGE_PROMPT, EVALUATE_PROMPT, REPORT_PROMPT,
)
from backend.core.llm_factory import get_llm, get_structured_llm
from backend.core.memory import (
    should_trigger_summary, compress_to_summary, trim_messages_to_window,
)
from backend.core.logger import get_logger

logger = get_logger(__name__)

DEFAULT_MAX_TURNS = 40                     # 一轮照护流程的总轮数上限
MAX_FOLLOWUP_PER_QUESTION = 2             # 同一问题最多追问几次

# 异常子模式轮数上下限（独立于日常照护的 stage_turn_count）
ABNORMAL_MIN_TURNS = 2                      # 异常至少 2 轮（防偷懒跳过处置）
ABNORMAL_MAX_TURNS = 6                      # 异常最多 6 轮（防发散拖延）

# 各阶段「最少 / 最多」轮数（check_stage 用；日常照护内含异常子模式）
STAGE_MIN_TURNS = {
    CareStage.ADMISSION_ASSESSMENT.value: 3,   # 入住评估至少 3 轮
    CareStage.DAILY_CARE.value: 8,             # 日常照护至少 8 轮（含异常处理轮数）
    CareStage.PERIODIC_REVIEW.value: 3,        # 阶段复评至少 3 轮
    CareStage.DISCHARGE_ASSESSMENT.value: 2,   # 出院评估至少 2 轮
}
STAGE_MAX_TURNS = {
    CareStage.ADMISSION_ASSESSMENT.value: 8,   # 入住评估最多 8 轮
    CareStage.DAILY_CARE.value: 15,            # 日常照护最多 15 轮
    CareStage.PERIODIC_REVIEW.value: 6,        # 阶段复评最多 6 轮
    CareStage.DISCHARGE_ASSESSMENT.value: 5,   # 出院评估最多 5 轮
}

# 阶段顺序（流转方向；异常不再作为独立阶段）
STAGE_ORDER = [
    CareStage.ADMISSION_ASSESSMENT.value,
    CareStage.DAILY_CARE.value,
    CareStage.PERIODIC_REVIEW.value,
    CareStage.DISCHARGE_ASSESSMENT.value,
    CareStage.FINISHED.value,
]

# 各阶段的 Prompt 映射（日常照护按 daily_care_mode 在 DAILY/ABNORMAL 间切换）
STAGE_PROMPTS = {
    CareStage.ADMISSION_ASSESSMENT.value: ADMISSION_PROMPT,
    CareStage.DAILY_CARE.value: DAILY_CARE_PROMPT,
    CareStage.PERIODIC_REVIEW.value: REVIEW_PROMPT,
    CareStage.DISCHARGE_ASSESSMENT.value: DISCHARGE_PROMPT,
}

# 触发异常子模式的关键词（命中任一即从 normal 切到 abnormal）
# 偏保守：只放明确的"照护中发现异常"信号词，
# 通用词如"疼痛""不舒服"会误触，故不放进来
ABNORMAL_KEYWORDS: tuple[str, ...] = (
    "发现压红", "皮肤压红", "皮肤破损", "皮肤破溃", "压疮",
    "胃液颜色异常", "胃液变色", "鼻饲异常", "鼻饲回抽",
    "进食量骤降", "进食量骤减", "进食明显减少",
    "精神萎靡", "精神不振", "嗜睡",
    "呛咳", "误吸", "呕吐", "呕血",
    "发热", "高烧", "体温骤升",
    "跌倒", "坠床", "摔伤",
    "面色异常", "面色苍白", "面色发紫", "紫绀",
    "出血", "渗血", "出血不止",
    "意识不清", "意识模糊", "昏迷",
    "抽搐", "癫痫发作",
)

# 结束异常子模式的关键词（护理员说"异常处理完了"回到常规照护）
ABNORMAL_DONE_KEYWORDS: tuple[str, ...] = (
    "异常处理完", "异常已处理", "处理完毕", "已处置", "继续照护",
    "继续日常照护", "回到照护", "异常已解决",
)


def _msg_text(msg: BaseMessage) -> str:
    """安全提取消息文本，兼容 str 和 list（多模态）content。"""
    content = msg.content
    if isinstance(content, list):
        return "".join(
            p.get("text", "") if isinstance(p, dict) else str(p)
            for p in content
        )
    return str(content)


def _build_system_content(summary: str | None = None) -> str:
    """构建注入了历史摘要的 SystemMessage 内容。"""
    content = SYSTEM_PROMPT
    if summary:
        content += f"\n\n【历史照护摘要】\n{summary}"
    return content


# ──────────────────────────────────────────────────────────────
# 节点1：load_context — 加载照护计划和历史
# ──────────────────────────────────────────────────────────────

async def load_context_node(state: CareFlowState) -> dict:
    """加载老人的照护计划和近期照护历史。

    照护计划来源（按优先级）：
      ① Pipeline 上游（assessment）注入的 care_plan；
      ② CarePlanProvider（默认本地库：老人的照护服务项 elder_service_items +
         最近一次评估派生的计划 care_plans；配了 SAAS_CARE_BASE_URL 就改读主系统）。

    改造说明：原来第 ③ 级是**硬编码 mock 计划**（"介助" + 5 条固定任务），照护历史更是
    无论计划来源如何都用两条写死的记录。结果是新入住/未评估的老人也会拿到一份看起来
    完整的假计划继续推进状态机。现在取不到就标记 `care_plan_missing`，
    由 notify_missing_plan 节点如实告知并结束本轮——"没有计划"是必须让人知道的状态。
    """
    elder_id = state.get("elder_id", "")
    tenant_id = state.get("tenant_id") or "tenant_default"
    care_plan_id = state.get("care_plan_id", "")

    from backend.core.care_plan_provider import CarePlanProviderError, get_care_plan_provider

    # 先取数：计划与照护历史都从 Provider 来（历史与计划同源，避免两处不一致）
    care_plan: dict | None = None
    care_history: list[dict] = []
    plan_source = "missing"
    try:
        bundle = await get_care_plan_provider().fetch(elder_id, tenant_id)
        care_plan, care_history, plan_source = bundle.plan, bundle.history, bundle.plan_source
    except CarePlanProviderError as exc:
        # 远端取不到 != 可以编数据：如实标记缺失，让上层提示人工处理
        logger.error("load_context.provider_failed", elder_id=elder_id, error=str(exc))
    except Exception as exc:  # noqa: BLE001
        logger.warning("load_context.care_plan_db_failed",
                       elder_id=elder_id, error=str(exc))

    # ① 上游（Pipeline）传入的 care_plan 覆盖 Provider 的计划（上游是本次请求的显式入参）
    upstream_care_plan = state.get("care_plan")
    if upstream_care_plan:
        care_plan = upstream_care_plan
        plan_source = "pipeline"

    # 取不到数据时用空 dict 占位，真实状态由 care_plan_missing 表达（下游 .get 不报错）
    care_plan_missing = not care_plan
    if care_plan_missing:
        care_plan = {"plan_id": care_plan_id, "elder_id": elder_id, "daily_tasks": []}

    logger.info(
        "load_context.care_plan_loaded",
        elder_id=elder_id, source=plan_source,
        care_level=care_plan.get("care_level"),
        daily_tasks_count=len(care_plan.get("daily_tasks", [])),
        service_items=len(care_plan.get("service_items") or []),
        missing=care_plan_missing,
    )

    logger.info("load_context.care_history_loaded", elder_id=elder_id, days=len(care_history))

    # 当天照护计划（含执行状态）：日常照护阶段要知道"今天还剩哪几项没做"
    today_plan: list[dict] = []
    plan_progress: dict = {}
    if not care_plan_missing:
        try:
            today_plan = await get_care_plan_provider().today_plan(elder_id, tenant_id)
            from backend.db.repositories import daily_plan_progress
            plan_progress = daily_plan_progress(today_plan)
        except Exception as exc:  # noqa: BLE001
            # 当天计划取不到不影响对话：只是提示词里少一段进度
            logger.warning("load_context.today_plan_failed", elder_id=elder_id, error=str(exc))

    # 初始化阶段（如果未设置）
    current_stage = state.get("current_stage", CareStage.ADMISSION_ASSESSMENT.value)

    logger.info("load_context.done", elder_id=elder_id, stage=current_stage)

    return {
        "care_plan": care_plan,
        "care_plan_missing": care_plan_missing,
        "plan_source": plan_source,
        "care_history": care_history,
        "today_plan": today_plan,
        "plan_progress": plan_progress,
        "current_stage": current_stage,
        "max_turns": state.get("max_turns", DEFAULT_MAX_TURNS),
    }


def _format_today_plan(today_plan: list[dict], progress: dict) -> str:
    """把当天计划压成几行给 prompt 用（让指导落到"今天还剩什么没做"）。"""
    if not today_plan:
        return "（今天没有可执行的照护计划条目）"
    lines = [
        f"- [{ {'done': '已完成', 'missed': '未完成', 'planned': '待执行'}.get(p.get('status'), '待执行') }]"
        f" {p.get('project_name', '')}"
        + (f"（计划时间 {p['nurse_time']}）" if p.get("nurse_time") else "")
        + (f" 执行人 {p['carer_name']}" if p.get("carer_name") else "")
        for p in today_plan
    ]
    head = (f"今日共 {progress.get('total', 0)} 项：已完成 {progress.get('done', 0)}、"
            f"未完成 {progress.get('missed', 0)}、待执行 {progress.get('pending', 0)}")
    return head + "\n" + "\n".join(lines)


# ──────────────────────────────────────────────────────────────
# 节点1b：notify_missing_plan — 照护计划缺失时如实告知
# ──────────────────────────────────────────────────────────────

async def notify_missing_plan_node(state: CareFlowState) -> dict:
    """老人没有照护计划时，明确告诉使用方"流程无法开始"，而不是编一份计划继续跑。

    什么情况会走到这里：该老人在业务系统里既没有登记照护服务项，也没有评估派生的计划
    （典型是新入住还没建档、或评估还没做）。这时正确的动作是去补数据，而不是让 AI
    凭空生成一套照护任务——照护项目与频次是机构业务配置，AI 无权代替。
    """
    elder_id = state.get("elder_id", "")
    content = (
        f"老人 {elder_id or '（未指定）'} 当前没有照护计划，无法开始照护流程。\n\n"
        "可能原因：尚未完成能力评估，或机构侧还没有登记照护服务项。\n"
        "请先完成其一后再开始：\n"
        "1. 上传能力评估表，由评估结论生成照护计划；\n"
        "2. 在机构系统里为该老人登记照护服务项（项目 / 频次 / 护理时间段）。"
    )
    logger.warning("care_flow.plan_missing", elder_id=elder_id)
    return {
        # 同时写进 messages：/start 与会话接口是从最后一条 AI 消息取展示文本的
        "messages": [AIMessage(content=content)],
        "answer": content,
        "fallback_used": True,
        "structured_output": {
            "plan_missing": True,
            "elder_id": elder_id,
            "message": content,
        },
    }


# ──────────────────────────────────────────────────────────────
# 节点2：check_stage — 检查阶段是否需要流转 + 日常照护内子模式切换
# ──────────────────────────────────────────────────────────────

def _converge_under_discharge(current_stage: str, state: CareFlowState) -> str:
    """退住工单驱动的阶段收敛路由 —— 统一并入「复评 → 出院评估」主干道。

    与 STAGE_ORDER 顺序推进的区别：
      - 入住评估 / 日常照护：不走"下一阶段"，直插阶段复评（离院前综合评估），
        日常照护不再开新的照护循环；
      - 阶段复评：转出院评估；
      - 出院评估：走完 → FINISHED（触发报告生成）。

    设计取舍：收到工单说明老人即将离院，继续推进日常照护/复评循环已无意义；
    但仍保留一次「阶段复评」作为离院前综合评估，再进入出院评估，不直接跳终态。
    """
    elder_id = state.get("elder_id")
    if current_stage in (CareStage.ADMISSION_ASSESSMENT.value,
                         CareStage.DAILY_CARE.value):
        logger.info("check_stage.converge_to_review",
                    elder_id=elder_id, from_stage=current_stage)
        return CareStage.PERIODIC_REVIEW.value
    if current_stage == CareStage.PERIODIC_REVIEW.value:
        logger.info("check_stage.discharge_by_order", elder_id=elder_id)
        return CareStage.DISCHARGE_ASSESSMENT.value
    # DISCHARGE_ASSESSMENT 及兜底：出院评估走完直接终态
    return CareStage.FINISHED.value


async def check_stage_node(state: CareFlowState) -> dict:
    """
    两层决策：
        层1 - 日常照护内的子模式切换（normal ⇄ abnormal）
        层2 - 阶段流转（按 STAGE_ORDER 顺序推进）

    子模式切换条件：
        normal → abnormal：护理员输入命中 ABNORMAL_KEYWORDS
        abnormal → normal：护理员输入命中 ABNORMAL_DONE_KEYWORDS 或异常轮数到上限

    阶段流转条件：
        1. 达到阶段最大轮数
        2. 护理员输入"结束照护"等关键词且已达阶段最少轮数
        3. 达到总轮数上限
    """
    current_stage = state.get("current_stage", CareStage.ADMISSION_ASSESSMENT.value)
    stage_turn_count = state.get("stage_turn_count", 0)
    total_turn_count = state.get("total_turn_count", 0)
    max_turns = state.get("max_turns", DEFAULT_MAX_TURNS)
    daily_care_mode = state.get("daily_care_mode", DailyCareMode.NORMAL.value)
    abnormal_turn_count = state.get("abnormal_turn_count", 0)
    discharge_order = state.get("discharge_order")  # SaaS 注入的退住工单（None=续住）

    # 获取最近一条消息（护理员输入）
    messages = state.get("messages", [])
    last_input = ""
    if messages:
        last_msg = messages[-1]
        last_input = _msg_text(last_msg).lower()

    if current_stage == CareStage.FINISHED.value:
        return {}  # 已结束

    # ── 层1：日常照护子模式切换（不切阶段，只切模式） ──
    if current_stage == CareStage.DAILY_CARE.value:
        if daily_care_mode == DailyCareMode.NORMAL.value:
            # normal → abnormal：关键词触发
            if any(kw in last_input for kw in ABNORMAL_KEYWORDS):
                logger.info("check_stage.abnormal_triggered",
                            query=last_input[:40])
                return {
                    "daily_care_mode": DailyCareMode.ABNORMAL.value,
                    "abnormal_turn_count": 0,
                }
        else:  # abnormal 模式
            # abnormal → normal：关键词触发 或 轮数到上限
            if any(kw in last_input for kw in ABNORMAL_DONE_KEYWORDS):
                logger.info("check_stage.abnormal_done_by_keyword",
                            turns=abnormal_turn_count)
                return {
                    "daily_care_mode": DailyCareMode.NORMAL.value,
                    "abnormal_turn_count": 0,
                }
            if abnormal_turn_count >= ABNORMAL_MAX_TURNS:
                logger.info("check_stage.abnormal_done_by_max",
                            turns=abnormal_turn_count)
                return {
                    "daily_care_mode": DailyCareMode.NORMAL.value,
                    "abnormal_turn_count": 0,
                }

    # ── 层2：阶段流转（异常模式下不让用户主动结束阶段——异常没处理完不能跳走） ──
    finish_keywords = ["结束照护", "完成照护", "进入下一阶段", "结束本阶段", "阶段完成"]
    user_wants_finish = any(kw in last_input for kw in finish_keywords)

    stage_max = STAGE_MAX_TURNS.get(current_stage, 10)
    stage_min = STAGE_MIN_TURNS.get(current_stage, 1)
    reached_max = stage_turn_count >= stage_max
    reached_min = stage_turn_count >= stage_min
    reached_total_max = total_turn_count >= max_turns

    # 异常子模式内，用户说"结束本阶段"不生效——必须先处理完异常回到 normal 才能流转
    # （异常 max 到了会自动切回 normal，下一轮才能正常流转）
    in_abnormal = (current_stage == CareStage.DAILY_CARE.value
                   and daily_care_mode == DailyCareMode.ABNORMAL.value)
    if in_abnormal:
        user_wants_finish = False

    # 工单驱动的收敛触发：存在退住工单时，当前阶段达到 min 即可流转（不必等 max），
    # 由 _converge_under_discharge 决定去向（统一并入 复评 → 出院评估 主干道）。
    # 异常子模式内不收敛（异常未处理完不能因工单被勾走），仍走原有 max 流转兜底。
    discharge_converge = bool(discharge_order) and reached_min and not in_abnormal

    if (reached_total_max
            or (user_wants_finish and reached_min)
            or reached_max
            or discharge_converge):
        # 总轮数到上限 → 直接结束（老人不出院也要结束本周期、生成报告、下周期重新开始）
        if reached_total_max:
            next_stage = CareStage.FINISHED.value
        elif discharge_order:
            # 有退住工单：按当前阶段收敛到 复评 → 出院评估 主干道
            next_stage = _converge_under_discharge(current_stage, state)
        elif current_stage == CareStage.PERIODIC_REVIEW.value:
            # 阶段复评后无工单 → 回日常照护开新循环
            # 养老机构常态是老人长期住着不出院，"出院"是少数业务事件，不该靠轮数强制推进
            next_stage = CareStage.DAILY_CARE.value
            logger.info("check_stage.loop_back_to_daily_care",
                        elder_id=state.get("elder_id"))
        else:
            # 其他阶段按 STAGE_ORDER 顺序推进
            try:
                current_idx = STAGE_ORDER.index(current_stage)
                next_stage = STAGE_ORDER[current_idx + 1]
            except (ValueError, IndexError):
                next_stage = CareStage.FINISHED.value

        logger.info("check_stage.transition",
                    from_stage=current_stage, to_stage=next_stage,
                    stage_turns=stage_turn_count, total_turns=total_turn_count,
                    has_discharge_order=bool(discharge_order),
                    converged=discharge_converge)

        return {
            "current_stage": next_stage,
            "stage_turn_count": 0,
            # 重置子模式状态（阶段流转后新阶段默认 normal）
            "daily_care_mode": DailyCareMode.NORMAL.value,
            "abnormal_turn_count": 0,
        }

    return {}


# ──────────────────────────────────────────────────────────────
# 节点3：evaluate_response — 评估护理员回答质量
# ──────────────────────────────────────────────────────────────

async def evaluate_response_node(state: CareFlowState) -> dict:
    """用 LLM 评估护理员的回答/操作质量。"""
    messages = state.get("messages", [])
    if not messages:
        return {"last_response_quality": ResponseQuality.NO_RESPONSE.value}

    last_msg = messages[-1]
    caregiver_response = _msg_text(last_msg)

    # 如果是 AI 消息（首次进入），不需要评估
    if isinstance(last_msg, AIMessage):
        return {"last_response_quality": ResponseQuality.GOOD.value}

    current_stage = state.get("current_stage", "")
    care_plan = state.get("care_plan", {})

    # 简单规则评估
    if not caregiver_response.strip():
        return {"last_response_quality": ResponseQuality.NO_RESPONSE.value}

    # 用 LLM 评估
    try:
        prompt = EVALUATE_PROMPT.format(
            current_stage=current_stage,
            care_plan=json.dumps(care_plan, ensure_ascii=False),
            caregiver_response=caregiver_response[:500],
        )
        llm = get_llm("care_flow", temperature=0)
        resp = await llm.ainvoke([HumanMessage(content=prompt)])
        quality = _msg_text(resp).strip().lower()
        if quality not in ("good", "adequate", "poor", "no_response"):
            quality = ResponseQuality.ADEQUATE.value
        logger.info("evaluate_response.done", quality=quality)
        return {"last_response_quality": quality}
    except Exception as e:
        logger.warning("evaluate_response.fallback", error=str(e))
        return {"last_response_quality": ResponseQuality.ADEQUATE.value}


# ──────────────────────────────────────────────────────────────
# 节点4：generate_response — 生成 AI 响应
# ──────────────────────────────────────────────────────────────

async def generate_response_node(state: CareFlowState) -> dict:
    """根据当前阶段生成 AI 指导响应。

    日常照护阶段按 daily_care_mode 在 DAILY_CARE_PROMPT 和 ABNORMAL_PROMPT 间切换：
      - normal：日常照护指导
      - abnormal：异常处置指导（评估严重度、处置步骤、观察指标、紧急程度）
    """
    current_stage = state.get("current_stage", CareStage.ADMISSION_ASSESSMENT.value)
    care_plan = state.get("care_plan", {})
    care_history = state.get("care_history", [])
    existing_summary = state.get("existing_summary")
    daily_care_mode = state.get("daily_care_mode", DailyCareMode.NORMAL.value)

    # 获取护理员输入
    messages = state.get("messages", [])
    caregiver_input = ""
    if messages:
        last_msg = messages[-1]
        caregiver_input = _msg_text(last_msg)

    # 获取阶段对应 Prompt；日常照护按子模式选
    prompt_template = STAGE_PROMPTS.get(current_stage, DAILY_CARE_PROMPT)
    if (current_stage == CareStage.DAILY_CARE.value
            and daily_care_mode == DailyCareMode.ABNORMAL.value):
        prompt_template = ABNORMAL_PROMPT

    prompt = prompt_template.format(
        care_plan=json.dumps(care_plan, ensure_ascii=False),
        care_history=json.dumps(care_history, ensure_ascii=False),
        today_plan=_format_today_plan(state.get("today_plan") or [],
                                      state.get("plan_progress") or {}),
        caregiver_input=caregiver_input[:500],
    )

    try:
        # 裁剪消息窗口：保留最近 10 轮（与 SOP 问答统一，长会话靠摘要补历史）
        trimmed_messages = trim_messages_to_window(messages, window_size=10)
        llm = get_llm("care_flow", temperature=0.3, streaming=True)
        # 用 astream 替代 ainvoke：逐 token 收集，首 token 更快到达
        chunks_text = []
        async for chunk in llm.astream(
            [SystemMessage(content=_build_system_content(existing_summary))]
            + trimmed_messages
            + [HumanMessage(content=prompt)]
        ):
            text = _msg_text(chunk)
            if text:
                chunks_text.append(text)
        ai_response = "".join(chunks_text)
    except Exception as e:
        logger.error("generate_response.failed", error=str(e))
        ai_response = "照护指导生成失败，请稍后重试。"

    # 更新轮数计数：异常子模式独立累计 abnormal_turn_count
    stage_turn_count = state.get("stage_turn_count", 0) + 1
    total_turn_count = state.get("total_turn_count", 0) + 1
    update: dict = {
        "messages": [AIMessage(content=ai_response)],
        "stage_turn_count": stage_turn_count,
        "total_turn_count": total_turn_count,
        "should_summarize": should_trigger_summary(messages, state.get("summary_message_count", 0)),
    }
    if (current_stage == CareStage.DAILY_CARE.value
            and daily_care_mode == DailyCareMode.ABNORMAL.value):
        update["abnormal_turn_count"] = state.get("abnormal_turn_count", 0) + 1
    return update


# ──────────────────────────────────────────────────────────────
# 节点5：generate_report — 生成照护评估报告
# ──────────────────────────────────────────────────────────────

async def generate_report_node(state: CareFlowState) -> dict:
    """用 LLM 结构化输出生成照护评估报告。"""
    care_plan = state.get("care_plan", {})
    care_history = state.get("care_history", [])
    messages = state.get("messages", [])

    # 生成对话摘要
    conversation_summary = ""
    if messages:
        try:
            conversation_summary = await compress_to_summary(
                messages, state.get("existing_summary")
            )
        except Exception as e:
            logger.warning("generate_report.summary_failed", error=str(e))

    # 阶段进展
    stage_progress = f"总轮数: {state.get('total_turn_count', 0)}, 当前阶段: {state.get('current_stage', '')}"

    prompt = REPORT_PROMPT.format(
        care_plan=json.dumps(care_plan, ensure_ascii=False),
        care_history=json.dumps(care_history, ensure_ascii=False),
        conversation_summary=conversation_summary or "（无摘要）",
        stage_progress=stage_progress,
    )

    try:
        structured_llm = get_structured_llm("care_flow", CareReport)
        result: CareReport = await structured_llm.ainvoke([
            SystemMessage(content=SYSTEM_PROMPT),
            HumanMessage(content=prompt),
        ])
        report_dict = result.model_dump()
        logger.info("generate_report.done", score=report_dict.get("overall_score"))
    except Exception as e:
        logger.error("generate_report.failed", error=str(e))
        report_dict = {
            "dimensions": [],
            "overall_score": 0,
            "strengths": [],
            "improvements": ["报告生成失败，请人工评估"],
            "care_suggestions": [],
            "next_review_date": datetime.now().strftime("%Y-%m-%d"),
        }

    structured_output = {
        "care_plan_id": state.get("care_plan_id", ""),
        "elder_id": state.get("elder_id", ""),
        "report": report_dict,
    }

    return {"report": report_dict, "structured_output": structured_output}


# ──────────────────────────────────────────────────────────────
# 节点6：save_report — 保存报告（占位）
# ──────────────────────────────────────────────────────────────

async def save_report_node(state: CareFlowState) -> dict:
    """保存照护报告到数据库（尽力而为：失败不中断流程，仅记日志）。"""
    session_id = state.get("session_id", "")
    elder_id = state.get("elder_id", "")

    from backend.db.repositories import save_care_flow_report
    saved = await save_care_flow_report(
        session_id=session_id,
        elder_id=elder_id,
        care_plan_id=state.get("care_plan_id", ""),
        stage_reached=state.get("current_stage", ""),
        total_turns=state.get("total_turn_count", 0),
        report=state.get("report", {}),
        tenant_id=state.get("tenant_id") or "tenant_default",
    )
    logger.info("save_report.done", session_id=session_id, persisted=saved)

    # 下行通知：报告落库后落一条出站事件，由后台投递器回调 SaaS
    # （发件箱 + 重试，尽力而为——失败不影响流程本身；SaaS 未配回调地址则留在发件箱等配置）
    from backend.core.outbound import enqueue_care_flow_finished
    await enqueue_care_flow_finished(
        session_id=session_id,
        elder_id=elder_id,
        tenant_id=state.get("tenant_id") or "tenant_default",
        care_plan_id=state.get("care_plan_id", ""),
        stage_reached=state.get("current_stage", ""),
        total_turns=state.get("total_turn_count", 0),
        report=state.get("report", {}),
        discharge_driven=bool(state.get("discharge_order")),
    )
    return {}


# ──────────────────────────────────────────────────────────────
# 节点7：save_memory — 保存记忆（占位）
# ──────────────────────────────────────────────────────────────

async def save_memory_node(state: CareFlowState) -> dict:
    """保存会话记忆：需要时压缩历史并**写回 existing_summary**。

    写回是关键：generate_response 会读 existing_summary 拼进系统提示，
    只生成不写回等于每轮都从零开始、还白花一次 LLM 调用。
    写回后随检查点落库，服务重启后长会话摘要仍然可用。
    """
    messages = state.get("messages", [])
    update: dict = {}

    if state.get("should_summarize") and messages:
        try:
            summary = await compress_to_summary(messages, state.get("existing_summary"))
            update["existing_summary"] = summary
            update["summary_message_count"] = len(messages)
            logger.info("save_memory.summary_saved", length=len(summary),
                        covered_messages=len(messages))
        except Exception as e:
            logger.warning("save_memory.summary_failed", error=str(e))

    return update
