# backend/agents/assessment/nodes.py
# 评估 Agent 的所有节点函数

import asyncio
import json

from langchain_core.messages import HumanMessage, SystemMessage

from backend.agents.assessment.state import (
    AssessmentState, ElderBasicInfo, AssessmentDimension,
    IssueList, AssessmentSummary, DiagnosisAndSummary,
)
from backend.agents.assessment.prompts import (
    SYSTEM_PROMPT, EXTRACT_PROMPT, DIMENSION_PROMPTS,
    ISSUE_DIAGNOSE_PROMPT, SUMMARY_PROMPT, DIAGNOSE_AND_SUMMARY_PROMPT,
)
from backend.core.llm_factory import get_structured_llm, get_llm
from backend.core.logger import get_logger

logger = get_logger(__name__)


# ── 五维度定义（名称 / 权重 / 评分侧重）。权重之和 = 1.0 ──
FIVE_DIMENSIONS = [
    {"key": "adl",        "name": "ADL日常活动能力", "weight": 0.30,
     "focus": "进食、穿衣、如厕、移动、洗澡等日常生活自理程度"},
    {"key": "cognition",  "name": "认知功能",       "weight": 0.25,
     "focus": "定向力、记忆力、计算力、判断力、语言能力等认知状态"},
    {"key": "nutrition",   "name": "营养状态",       "weight": 0.20,
     "focus": "BMI、进食量、体重变化、血清蛋白等营养指标"},
    {"key": "fall_risk",   "name": "跌倒风险",       "weight": 0.15,
     "focus": "跌倒史、步态、用药、视力、辅助器具等跌倒风险因素"},
    {"key": "social",      "name": "社交参与",       "weight": 0.10,
     "focus": "集体活动参与度、人际交流、情绪状态、抑郁筛查等"},
]


def _get_message_content(msg) -> str:
    """统一获取消息文本内容（兼容 text 属性和 content 属性）"""
    if hasattr(msg, "text") and not callable(getattr(msg, "text", None)):
        return msg.text
    if isinstance(msg.content, str):
        return msg.content
    return str(msg.content)


def _build_structured_summary(structured: dict) -> str:
    """把结构化数据浓缩成几行摘要，供评审使用（省 token）。"""
    lines = []
    if structured.get("name"):
        lines.append(f"姓名：{structured['name']}")
    if structured.get("age"):
        lines.append(f"年龄：{structured['age']}岁")
    if structured.get("gender"):
        lines.append(f"性别：{structured['gender']}")
    if structured.get("room_no"):
        lines.append(f"房间号：{structured['room_no']}")
    if structured.get("care_level"):
        lines.append(f"当前护理等级：{structured['care_level']}")
    return "\n".join(lines) if lines else "（结构化提取失败，请基于原文评审）"


def _empty_dimension_score(dim: dict) -> dict:
    """维度评审失败时的降级结果。"""
    return {
        "key": dim["key"], "dimension": dim["name"],
        "score": 50, "weight": dim["weight"],
        "issues": ["该维度评审失败，建议人工复核"],
        "suggestions": [], "evidence": "",
    }


# ──────────────────────────────────────────────────────────────
# 节点1：parse_pdf — 解析评估表 PDF
# ──────────────────────────────────────────────────────────────

def _sync_parse_pdf(pdf_path: str) -> dict:
    """同步 PDF 文本提取（线程池中运行），处理双栏布局。"""
    import fitz  # PyMuPDF
    doc = fitz.open(pdf_path)
    page_count = len(doc)
    all_text_parts = []

    for page in doc:
        blocks = page.get_text("blocks")
        text_blocks = [b for b in blocks if b[6] == 0]  # type==0 文字块
        if not text_blocks:
            continue
        page_width = page.rect.width
        midpoint = page_width / 2
        left_blocks = [b for b in text_blocks if b[0] < midpoint - 20]
        right_blocks = [b for b in text_blocks if b[0] >= midpoint - 20]
        is_two_column = (
            len(left_blocks) >= 2 and len(right_blocks) >= 2
            and len(right_blocks) / max(len(text_blocks), 1) > 0.3
        )
        if is_two_column:
            left_sorted = sorted(left_blocks, key=lambda b: b[1])
            right_sorted = sorted(right_blocks, key=lambda b: b[1])
            page_text = (
                "\n".join(b[4].strip() for b in left_sorted if b[4].strip())
                + "\n"
                + "\n".join(b[4].strip() for b in right_sorted if b[4].strip())
            )
        else:
            sorted_blocks = sorted(text_blocks, key=lambda b: b[1])
            page_text = "\n".join(b[4].strip() for b in sorted_blocks if b[4].strip())
        all_text_parts.append(page_text)

    doc.close()
    raw_text = "\n\n---PAGE BREAK---\n\n".join(all_text_parts)
    return {"raw_text": raw_text, "page_count": page_count}


async def parse_pdf_node(state: AssessmentState) -> dict:
    """异步节点：线程池跑同步解析，避免阻塞事件循环。"""
    pdf_path = state["pdf_local_path"]
    try:
        loop = asyncio.get_running_loop()
        result = await loop.run_in_executor(None, _sync_parse_pdf, pdf_path)
        raw_text, page_count = result["raw_text"], result["page_count"]
        if len(raw_text.strip()) < 200:  # 疑似扫描件/图片 PDF
            logger.warning("parse_pdf.text_too_short", text_length=len(raw_text.strip()))
        logger.info("parse_pdf.done", page_count=page_count, text_length=len(raw_text))
        return {"raw_text": raw_text, "page_count": page_count}
    except Exception as e:
        logger.error("parse_pdf.failed", error=str(e))
        raise


# ──────────────────────────────────────────────────────────────
# 节点2：extract_structured — LLM 结构化提取老人基本信息
# ──────────────────────────────────────────────────────────────

async def extract_structured_node(state: AssessmentState) -> dict:
    """用 LLM 把评估表文本提取成结构化老人基本信息。"""
    raw_text = state["raw_text"]
    text_for_llm = raw_text[:4000] if len(raw_text) > 4000 else raw_text

    prompt = EXTRACT_PROMPT.format(assessment_text=text_for_llm)
    llm = get_llm("assessment", temperature=0)

    structured_dict = None
    for attempt in range(2):
        try:
            response = await llm.ainvoke([
                SystemMessage(content=SYSTEM_PROMPT),
                HumanMessage(content=prompt),
            ])
            content = (
                response.content
                if hasattr(response, "content")
                else str(response)
            ).strip()
            logger.debug("extract_structured.raw_response", attempt=attempt, content_preview=content[:200])

            # 清理 markdown 代码块
            if "```" in content:
                blocks = content.split("```")
                for block in blocks[1::2]:
                    block = block.strip()
                    if block.startswith("json"):
                        block = block[4:].strip()
                    content = block
                    break

            raw_dict = json.loads(content)
            structured_dict = ElderBasicInfo(**raw_dict).model_dump()
            break
        except json.JSONDecodeError as e:
            logger.warning("extract_structured.json_error", attempt=attempt, error=str(e))
            if attempt == 0:
                await asyncio.sleep(1)
        except Exception as e:
            logger.warning("extract_structured.error", attempt=attempt, error=str(e))
            if attempt == 0:
                await asyncio.sleep(1)

    if structured_dict is None:
        logger.warning("extract_structured.fallback")
        structured_dict = ElderBasicInfo(name="未能提取", age=0, gender="未知").model_dump()

    logger.info("extract_structured.done", name=structured_dict.get("name", ""))
    return {"structured": structured_dict}


# ──────────────────────────────────────────────────────────────
# 节点3：run_five_dimensions — 五维度并行评审
# ──────────────────────────────────────────────────────────────

async def run_five_dimensions_node(state: AssessmentState) -> dict:
    """五维度并行评审：asyncio.gather 同时评，算加权综合分。"""
    raw_text = state["raw_text"]
    structured = state.get("structured") or {}
    structured_summary = _build_structured_summary(structured)

    async def review_one_dimension(dim: dict) -> dict:
        prompt_template = DIMENSION_PROMPTS.get(dim["key"], "")
        if not prompt_template:
            return _empty_dimension_score(dim)
        prompt = prompt_template.format(
            assessment_text=raw_text[:3000],
            structured_summary=structured_summary,
            focus=dim["focus"],
        )
        for attempt in range(2):
            try:
                structured_llm = get_structured_llm("assessment", AssessmentDimension)
                result: AssessmentDimension = await structured_llm.ainvoke([
                    SystemMessage(content=SYSTEM_PROMPT),
                    HumanMessage(content=prompt),
                ])
                d = result.model_dump()
                d["dimension"], d["weight"], d["key"] = dim["name"], dim["weight"], dim["key"]
                return d
            except Exception as e:
                if attempt == 0:
                    logger.warning("five_dimensions.dimension_retry", dimension=dim["name"], error=str(e))
                    await asyncio.sleep(1)
                else:
                    logger.warning("five_dimensions.dimension_failed", dimension=dim["name"], error=str(e))
                    return _empty_dimension_score(dim)

    tasks = [review_one_dimension(dim) for dim in FIVE_DIMENSIONS]
    dimension_scores = await asyncio.gather(*tasks)  # 并行
    weighted_score = sum(d["score"] * d["weight"] for d in dimension_scores)
    logger.info("five_dimensions.done", weighted_score=round(weighted_score, 2),
                scores={d["key"]: d["score"] for d in dimension_scores})
    return {"dimension_scores": list(dimension_scores), "weighted_score": round(weighted_score, 2)}


# ──────────────────────────────────────────────────────────────
# 节点4：diagnose_issues — 逐条问题诊断
# ──────────────────────────────────────────────────────────────

async def diagnose_issues_node(state: AssessmentState) -> dict:
    """汇总维度问题 → LLM 结构化生成问题清单 → 按优先级排序。"""
    dimension_scores = state.get("dimension_scores", [])
    raw_text = state["raw_text"]
    structured = state.get("structured") or {}

    all_raw_issues = []
    for dim in dimension_scores:
        for issue_text in dim.get("issues", []):
            all_raw_issues.append(f"[{dim['dimension']}] {issue_text}")
    raw_issues_text = "\n".join(f"- {i}" for i in all_raw_issues) or "（暂无）"

    prompt = ISSUE_DIAGNOSE_PROMPT.format(
        structured_summary=_build_structured_summary(structured),
        assessment_text=raw_text[:3000],
        raw_issues=raw_issues_text,
    )

    try:
        structured_llm = get_structured_llm("assessment", IssueList)
        result: IssueList = await structured_llm.ainvoke([
            SystemMessage(content=SYSTEM_PROMPT), HumanMessage(content=prompt)])
        issues = [item.model_dump() for item in result.items]
    except Exception as e:
        logger.warning("diagnose_issues.failed", error=str(e))
        issues = [
            {"priority": "medium", "dimension": dim["dimension"], "description": issue,
             "location": "评估表全文", "suggestion": "请参考评审建议制定照护措施"}
            for dim in dimension_scores for issue in dim.get("issues", [])
        ]

    priority_order = {"high": 0, "medium": 1, "low": 2}
    issues.sort(key=lambda x: priority_order.get(x.get("priority", "low"), 2))
    logger.info("diagnose_issues.done", total=len(issues),
                high=sum(1 for i in issues if i.get("priority") == "high"))
    return {"issues": issues}


# ──────────────────────────────────────────────────────────────
# 节点 4.5：diagnose_and_summarize — 合并诊断+评价（优化：省 1 轮 LLM 调用）
# ──────────────────────────────────────────────────────────────

async def diagnose_and_summarize_node(state: AssessmentState) -> dict:
    """合并 diagnose_issues + generate_summary 为单次 LLM 调用。

    原来两步串行（先诊断问题，再用问题生成总结），现在用一次结构化调用同时产出。
    省掉 1 轮 LLM 调用（~15-30s）。
    """
    dimension_scores = state.get("dimension_scores", [])
    raw_text = state["raw_text"]
    structured = state.get("structured") or {}
    weighted_score = state.get("weighted_score", 0.0)

    # 汇总各维度原始问题
    all_raw_issues = []
    for dim in dimension_scores:
        for issue_text in dim.get("issues", []):
            all_raw_issues.append(f"[{dim['dimension']}] {issue_text}")
    raw_issues_text = "\n".join(f"- {i}" for i in all_raw_issues) or "（暂无）"

    # 维度评分摘要
    scores_text = "\n".join(
        f"- {d['dimension']}：{d['score']}分（权重{int(d['weight'] * 100)}%）"
        for d in dimension_scores
    )

    prompt = DIAGNOSE_AND_SUMMARY_PROMPT.format(
        structured_summary=_build_structured_summary(structured),
        assessment_text=raw_text[:3000],
        raw_issues=raw_issues_text,
        scores_summary=scores_text,
        weighted_score=round(weighted_score, 1),
    )

    try:
        structured_llm = get_structured_llm("assessment", DiagnosisAndSummary)
        result: DiagnosisAndSummary = await structured_llm.ainvoke([
            SystemMessage(content=SYSTEM_PROMPT), HumanMessage(content=prompt)])
        issues = [item.model_dump() for item in result.issues]
        summary_dict = {
            "highlights": result.highlights,
            "core_improvements": result.core_improvements,
            "overall_comment": result.overall_comment,
            "care_level_recommendation": result.care_level_recommendation,
        }
    except Exception as e:
        logger.warning("diagnose_and_summarize.failed", error=str(e))
        # fallback：与原 diagnose_issues 兜底逻辑一致
        issues = [
            {"priority": "medium", "dimension": dim["dimension"], "description": issue,
             "location": "评估表全文", "suggestion": "请参考评审建议制定照护措施"}
            for dim in dimension_scores for issue in dim.get("issues", [])
        ]
        summary_dict = {
            "highlights": ["评估内容已完整提交"],
            "core_improvements": ["请参考各维度建议制定照护计划"],
            "overall_comment": f"综合评分 {round(weighted_score, 1)} 分，请参考各维度详细反馈。",
            "care_level_recommendation": "介助" if weighted_score < 80 else "自理",
        }

    # 按优先级排序
    priority_order = {"high": 0, "medium": 1, "low": 2}
    issues.sort(key=lambda x: priority_order.get(x.get("priority", "low"), 2))

    logger.info("diagnose_and_summarize.done",
                issues_count=len(issues),
                high_count=sum(1 for i in issues if i.get("priority") == "high"),
                care_level=summary_dict.get("care_level_recommendation"))

    return {"issues": issues, "summary": summary_dict}


# ──────────────────────────────────────────────────────────────
# 节点5：generate_summary — 整体评价（保留兼容，diagnose_and_summarize 已覆盖其功能）
# ──────────────────────────────────────────────────────────────

async def generate_summary_node(state: AssessmentState) -> dict:
    """综合结构化信息、评分、问题，生成面向护理团队的整体评价。"""
    structured = state.get("structured") or {}
    dimension_scores = state.get("dimension_scores", [])
    issues = state.get("issues", [])
    weighted_score = state.get("weighted_score", 0.0)

    high_issues = [i["description"] for i in issues if i.get("priority") == "high"][:5]
    high_issues_text = "\n".join(f"- {i}" for i in high_issues) or "（无高优先级问题）"
    scores_text = "\n".join(
        f"- {d['dimension']}：{d['score']}分（权重{int(d['weight'] * 100)}%）" for d in dimension_scores)

    prompt = SUMMARY_PROMPT.format(
        structured_summary=_build_structured_summary(structured),
        scores_summary=scores_text, weighted_score=round(weighted_score, 1),
        high_issues=high_issues_text,
    )
    structured_llm = get_structured_llm("assessment", AssessmentSummary)
    summary_dict = None
    for attempt in range(2):
        try:
            result = await structured_llm.ainvoke([
                SystemMessage(content=SYSTEM_PROMPT), HumanMessage(content=prompt)])
            if result is None:
                raise ValueError("structured output returned None")
            summary_dict = result.model_dump()
            break
        except Exception as e:
            if attempt == 0:
                logger.warning("generate_summary.retry", error=str(e))
                await asyncio.sleep(1)
            else:
                logger.warning("generate_summary.failed", error=str(e))
    if summary_dict is None:
        summary_dict = {
            "highlights": ["评估内容已完整提交"],
            "core_improvements": high_issues[:2] if high_issues else ["请参考各维度建议制定照护计划"],
            "overall_comment": f"综合评分 {round(weighted_score, 1)} 分，请参考各维度详细反馈。",
            "care_level_recommendation": "介助" if weighted_score < 80 else "自理",
        }
    logger.info("generate_summary.done", highlights_count=len(summary_dict.get("highlights", [])))
    return {"summary": summary_dict}


# ──────────────────────────────────────────────────────────────
# 节点6：save_results — 持久化评估结果到 assessment_results 表
# ──────────────────────────────────────────────────────────────

async def save_results_node(state: AssessmentState) -> dict:
    """保存评估结果到数据库（尽力而为：失败不中断流程，仅记日志）。"""
    assessment_id = state["assessment_id"]

    # summary 列存全量附加数据（structured / issues / summary / 护理等级建议）
    summary_payload = {
        "structured": state.get("structured"),
        "issues": state.get("issues", []),
        "summary": state.get("summary"),
    }

    from backend.db.repositories import save_assessment_result
    saved = await save_assessment_result(
        assessment_id=assessment_id,
        elder_id=state["elder_id"],
        dimension_scores=state.get("dimension_scores", []),
        weighted_score=state.get("weighted_score", 0),
        summary_payload=summary_payload,
        tenant_id=state.get("tenant_id") or "tenant_default",
    )
    logger.info("save_results.done", assessment_id=assessment_id, persisted=saved)

    # 留一份完整结果给上层（API 可直接用）
    structured_output = {
        "assessment_id": assessment_id,
        "elder_id": state["elder_id"],
        "structured": state.get("structured"),
        "weighted_score": state.get("weighted_score", 0),
        "dimension_scores": state.get("dimension_scores", []),
        "issues": state.get("issues", []),
        "summary": state.get("summary"),
        "persisted": saved,
    }

    return {"fallback_used": False, "structured_output": structured_output}
